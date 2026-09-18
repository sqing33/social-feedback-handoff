"""Production WSGI entry point for Waitress."""

from ai_service import start_worker
from social_feedback_backend import app, connect_db, init_db, seed_accounts_from_feedback

init_db()
seed_accounts_from_feedback()
start_worker(connect_db)
