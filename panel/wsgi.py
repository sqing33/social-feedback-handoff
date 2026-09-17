"""Production WSGI entry point for Waitress."""

from social_feedback_backend import app, init_db, seed_accounts_from_feedback

init_db()
seed_accounts_from_feedback()
