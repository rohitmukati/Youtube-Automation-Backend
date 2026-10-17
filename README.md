# Youtube-Automation-Backend

A FastAPI backend that lets a user sign up, connect many YouTube accounts (different Google emails), store videos and images once in a library called Artifacts, and publish to one or many connected channels from a single place.

Data is stored in Supabase (Postgres and Storage). Authentication is built in-house; Supabase Auth is not used.

## Project status

| Area | Status |
|---|---|
| Auth: signup, login, refresh, logout, forgot and reset password | Done and tested |
| YouTube accounts: connect, list, disconnect, token refresh | Not started |
| Artifacts: upload and manage videos and images | Not started |
| Multi-account upload jobs: title, description, thumbnail, status tracking | Not started |
| Deployment on GCP | Not started |

## Tech stack

- Python 3.12, FastAPI, Uvicorn
- SQLAlchemy 2 (async) with asyncpg, Supabase Postgres
- Argon2 password hashing, JWT access tokens, rotating refresh tokens
- SMTP (Gmail) for password reset codes
- Pydantic Settings for configuration
- Pytest, pytest-asyncio, httpx for tests

## Folder structure

```
main.py                  application entrypoint (run with: uvicorn main:app)
app/
  config.py              all settings: values from .env plus fixed values
  core/                  database, security, exceptions, handlers, logging, dependencies
  models/                SQLAlchemy tables
  schemas/               request and response models
  crud/                  database queries
  service/               business logic
  routes/                API endpoints
  integrations/          external systems (SMTP now, Google and Supabase Storage later)
  workers/               background jobs (upload jobs later)
  utils/                 small helpers: validators, date helpers, email templates
tests/                   automated tests
```

### Code conventions

- One file per feature in every layer. Everything about auth lives in `auth.py` of models, crud, schemas, service and routes. A new, unrelated feature (for example YouTube accounts) gets its own `youtube.py` in each layer.
- No comments in code, no emojis, user friendly error messages in English.
- Routes stay thin. Business rules live in `service`, queries live in `crud`.
- Nothing is hardcoded outside `app/config.py`. Secrets and environment specific values come from `.env`; fixed values are constants inside `config.py`.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies: `pip install -r requirements.txt` (use `requirements-dev.txt` to also run tests).
3. Create `.env` from `.env.example` and fill in the values below.
4. Run: `uvicorn main:app --reload`
5. Open `http://localhost:8000/docs`.

Tables are created automatically on startup.

## Environment variables

Only these live in `.env`. Everything else is fixed in `app/config.py`.

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Supabase Postgres connection string. Use the Session pooler string from Connect in the Supabase dashboard. |
| `JWT_SECRET_KEY` | Yes | Random secret, at least 32 characters. Also used to hash reset codes. |
| `SMTP_USERNAME` | For reset emails | Gmail address that sends the codes. Also used as the From address. |
| `SMTP_PASSWORD` | For reset emails | Gmail app password. |
| `SUPABASE_URL` | Artifacts | Project URL from the Supabase dashboard. |
| `SUPABASE_SERVICE_ROLE_KEY` | Artifacts | Secret key (`sb_secret_...`) from Settings, API Keys. Backend only. |
| `GOOGLE_CLIENT_ID` | YouTube accounts | OAuth client ID from Google Cloud. |
| `GOOGLE_CLIENT_SECRET` | YouTube accounts | OAuth client secret from Google Cloud. |
| `TOKEN_ENCRYPTION_KEY` | YouTube accounts | Key used to encrypt Google tokens before saving them. |
| `APP_ENV` | Deployment | `development` by default. Set `production` when deployed; this turns off `/docs`. |
| `CORS_ORIGINS` | Deployment | Comma separated frontend URLs. Defaults to localhost ports 3000 and 5173. |
| `GOOGLE_REDIRECT_URI` | Deployment | Defaults to the local callback URL. Set to the deployed callback URL in production. |

Generate a JWT secret: `python -c "import secrets; print(secrets.token_urlsafe(64))"`

Fixed values in `config.py`: app name, API prefix, database pool sizes, JWT algorithm, token lifetimes (access 30 minutes, refresh 7 days), password and OTP rules, SMTP host and port, storage bucket name and Google scopes.

## Supabase setup

1. Create a project at supabase.com and keep the database password safe.
2. Click Connect in the project, choose Session pooler and copy the string into `DATABASE_URL`. Replace the password placeholder. URL-encode special characters in the password.
3. Start the app and check `/health/ready`. It should report the database as up.
4. Enable row level security on the tables so the public Supabase REST API cannot read them. The backend connects as the database owner and is not affected:

```sql
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.password_reset_otps ENABLE ROW LEVEL SECURITY;
```

Repeat this for every new table.

## API reference

Base prefix: `/api/v1`. Interactive docs are at `/docs` outside production.

### Auth

| Method and path | Auth | Description |
|---|---|---|
| `POST /auth/signup` | No | Create an account and log in immediately. No OTP. |
| `POST /auth/login` | No | Log in. No OTP. |
| `POST /auth/refresh` | No | Exchange a refresh token for a new token pair. |
| `POST /auth/logout` | No | Revoke a refresh token. |
| `POST /auth/forgot-password` | No | Email a one-time code. Same response whether or not the email exists. |
| `POST /auth/reset-password` | No | Verify the code and set a new password. |
| `GET /auth/me` | Bearer token | Return the logged in user. |

### Health

| Method and path | Description |
|---|---|
| `GET /health` | Liveness check |
| `GET /health/ready` | Readiness check including the database |

### Examples

Signup request:

```json
{
  "name": "Rohan Mukati",
  "email": "user@example.com",
  "password": "StrongPass123",
  "phone": "+919876543210"
}
```

`phone` is optional. Signup and login both return:

```json
{
  "user": { "id": "...", "name": "...", "email": "...", "phone": null, "created_at": "..." },
  "tokens": {
    "access_token": "...",
    "refresh_token": "...",
    "token_type": "bearer",
    "expires_in": 1800
  }
}
```

Send the access token on protected routes as `Authorization: Bearer <access_token>`.

Reset password request:

```json
{ "email": "user@example.com", "otp": "123456", "new_password": "NewStrongPass456" }
```

### Error format

Every error uses one shape:

```json
{
  "success": false,
  "error": {
    "code": "validation_error",
    "message": "Some of the information you entered is not valid. Please review it and try again.",
    "details": [{ "field": "password", "message": "Password must contain at least one letter and one number." }]
  }
}
```

`details` appears only for validation errors. Use `error.code` in the frontend for logic and `error.message` for display.

## Security design

- Passwords are hashed with Argon2 and checked in a worker thread so the event loop is never blocked.
- Password rules: at least 8 characters with at least one letter and one number.
- Access tokens are short lived JWTs. Refresh tokens are random, stored only as hashes and rotated on every use. Reusing an old refresh token revokes all sessions of that user.
- Reset codes are 6 digits, valid for 10 minutes, single use, stored as keyed hashes, locked after 5 wrong attempts, with a 60 second resend cooldown.
- Login errors and forgot-password responses do not reveal whether an email is registered.
- A successful password reset revokes every active session.
- Secrets are read as `SecretStr` and never logged.

## Testing

```
pip install -r requirements-dev.txt
pytest
```

The tests use an in-memory SQLite database and a recording email service, so they never send real email or touch Supabase.

## Deployment notes (GCP)

- Target: Cloud Run. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT` (no `--reload`).
- Set `APP_ENV=production`, `CORS_ORIGINS` and `GOOGLE_REDIRECT_URI` as environment variables. Keep `DATABASE_URL`, `JWT_SECRET_KEY`, `SMTP_PASSWORD` and the Google and Supabase secrets in Secret Manager.
- A Dockerfile is still to be written.
- Tables are created with `create_all`, which only creates missing tables. It does not change existing ones. When a column must be added later, run the SQL manually in the Supabase SQL editor, or introduce Alembic before the schema changes often.

## What to build next

### Step 1: Google Cloud setup (manual)

1. Create a project in Google Cloud Console.
2. Enable YouTube Data API v3.
3. Configure the OAuth consent screen and add every Gmail you will connect as a test user.
4. Create an OAuth client of type Web application with the redirect URI `http://localhost:8000/api/v1/youtube/callback`.
5. Put the client ID and secret in `.env`.
6. Generate `TOKEN_ENCRYPTION_KEY` once the encryption library is added.

### Step 2: YouTube accounts feature (`youtube.py` in every layer)

- Table for connected channels: owner, channel ID, title, thumbnail, Google email, encrypted refresh token, scopes, status, connected time.
- `GET /youtube/connect` returns the Google consent URL with a signed state value to prevent forged callbacks.
- `GET /youtube/callback` exchanges the code, reads the channel with the YouTube API and saves the encrypted tokens.
- `GET /youtube/accounts` lists connected channels. `DELETE /youtube/accounts/{id}` disconnects and revokes the token.
- Automatic access token refresh, and a clear "reconnect needed" status when Google rejects the refresh token.
- Rule: one channel can be connected only once per user. A Google account with several brand channels is handled by connecting each channel separately.

### Step 3: Artifacts feature (`artifacts.py`)

- Private Supabase Storage bucket named `artifacts`.
- Table for files: owner, type (video or image), original name, MIME type, size, storage path, created time.
- Upload through signed upload URLs so large videos go directly from the client to Supabase and not through FastAPI.
- Endpoints: create upload URL, confirm upload, list with filters, get signed download URL, delete.
- Check the per-file size limit of your Supabase plan before relying on it for large videos. If it is too small, move video storage to Google Cloud Storage.

### Step 4: Upload jobs feature (`uploads.py`)

- A job holds the artifact, title, description, tags, thumbnail, privacy and optional publish time, plus a list of target channels.
- Each target is processed independently with its own status: pending, uploading, done, failed. One failing channel must not block the others.
- Resumable uploads to YouTube, thumbnail upload after the video, retry with backoff, and clear user facing failure messages.
- Run work in `workers/` so the API responds immediately. Start with a worker process that reads pending targets from the database; add a queue later if needed.
- Endpoints: create job, list jobs, job detail with per-channel status, retry failed targets, cancel.

### Step 5: Hardening and release

- Rate limiting on login, signup and forgot-password.
- Automated tests for every new feature in the same style as the auth tests.
- Dockerfile, CI pipeline and Cloud Run deployment.
- Structured logging and error monitoring.
- Alembic migrations once tables change often.
- Rotate every credential that was shared during development: database password, JWT secret, Gmail app password.

### Later ideas

Scheduled publishing, per-channel title and description overrides, bulk edit of published videos, playlists, analytics dashboard, team accounts.

## YouTube API limits to plan for

- The default quota is 10,000 units per Google Cloud project per day, shared by all your users. More quota is available only through Google's quota extension and audit form, which can take weeks.
- Upload cost: several 2026 sources report that Google reduced `videos.insert` from about 1,600 units to about 100 units in December 2025, while older pages still show 1,600. Check the official Quota Calculator before sizing your limits.
- A project that has not passed the YouTube API Services compliance audit may have API-uploaded videos locked as private. Plan the audit before public launch.
- While the OAuth consent screen is in Testing mode, refresh tokens expire after 7 days and only listed test users can connect. Publishing and verifying the app is required for real users.