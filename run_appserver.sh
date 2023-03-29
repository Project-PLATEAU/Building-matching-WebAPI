#!bash
gunicorn wsgi:app --workers=2 --bind=0.0.0.0:5000
