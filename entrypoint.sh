#!/usr/bin/env sh
flask db upgrade
gunicorn --config gunicorn.conf.py --preload pyfedi:app
