# TradeQuest (Paper Stocks)

Virtual paper stock trading webapp built with Django.

## Setup

1. Create/activate a virtual environment (we included one as `.venv`).
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Run migrations:
   - `python manage.py migrate`
4. (Optional, recommended) Set Django secret key:
   - `export DJANGO_SECRET_KEY="..."` (see `.env.example`)

## Run

- `python manage.py runserver`
- Open `http://localhost:8000/`

