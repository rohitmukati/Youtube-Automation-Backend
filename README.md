# Youtube-Automation-Backend

FastAPI backend for managing multiple YouTube accounts from one place. Data is stored in Supabase Postgres.

## Setup

1. Create a virtual environment and install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in the values.
3. Run the server: `uvicorn main:app --reload`
4. Open `http://localhost:8000/docs` for the API documentation.

Tables are created automatically on startup when `DB_AUTO_CREATE_TABLES=true`.

## Tests

Install `requirements-dev.txt` and run `pytest`.

## Auth endpoints (prefix `/api/v1`)

- `POST /auth/signup` and `POST /auth/login` return the user and tokens directly, with no OTP
- `POST /auth/refresh` rotates the refresh token
- `POST /auth/logout` revokes the refresh token
- `POST /auth/forgot-password` emails a one-time code
- `POST /auth/reset-password` verifies the code and sets a new password
- `GET /users/me` returns the logged in user