# Development Notes

This is an opinionated project where choices have been made which could surprise you. Contributions are welcome,
but please respect the established formatting and structural choices unless otherwise discussed.

For a general overview of how things work, see https://codeberg.org/rimu/pyfedi/src/branch/main/docs/developer_docs.

A git pre-commit hook can be used to automatically run [ruff](https://docs.astral.sh/ruff/) before committing code:

Save this as .git/hooks/pre-commit and make sure it's executable with chmod +x .git/hooks/pre-commit:

```
#!/bin/sh

# Absolute path to your ruff binary
RUFF="/home/rimu/Documents/piefed/venv/bin/ruff"

# Get all staged Python files
STAGED=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$')
if [ -z "$STAGED" ]; then
    exit 0
fi

echo "🔍 Running Ruff on staged files..."
$RUFF check $STAGED
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo "❌ Ruff found issues. Please fix them before committing."
    exit 1
else
    echo "✅ Ruff passed!"
    exit 0
fi
```

## ActivityPub Debugging

In `.env`, set `LOG_ACTIVITYPUB_TO_DB = 1` so that incoming federation activities will appear in `yourdomain.tld/admin/activities`.

Also at `yourdomain.tld/admin/misc` consider turning on "Log ActivityPub JSON for debugging" to see the full JSON of what is sent to your instance.

## Environment Setup

Ensure that the `FLASK_APP` environment variable is set to `pyfedi.py`.

## Celery

### Development
For running celery during development, run this:

```bash
celery -A celery_worker.celery worker --loglevel=INFO
```

### Production
On a production web server, celery is managed by systemd: `/etc/default/celeryd` and `/etc/systemd/system/celeryd.service`

```bash
sudo systemctl stop celeryd
sudo systemctl restart celeryd
# or
sudo service celeryd restart
```

**Check for celery-related problems by looking in `/var/log/celery`**

## Profiling

For profiling, use:
```bash
python profile_app.py
```
instead of:
```bash
flask run
```

Alternative method:
```bash
export FLASK_APP=profile_app.py  # instead of pyfedi.py
flask run
```

## Translations

See [The Flask Mega-Tutorial Part XIII: I18n and L10n](https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-xiii-i18n-and-l10n-2018)

Before doing any of the below, run `export FLASK_APP=pyfedi.py`

To add a new language:
```bash
flask translate init <language-code>
```

To update all languages after making changes to the `_()` and `_l()` language markers:
```bash
flask translate update
```

To compile all languages after updating the translation files:
```bash
flask translate compile
```

## SASS/SCSS

SASS is used to compile `.scss` files into `.css` files. See [https://sass-lang.com/dart-sass/](https://sass-lang.com/dart-sass/).

Get the latest version of the command line app from [https://github.com/sass/dart-sass/releases/](https://github.com/sass/dart-sass/releases/) as old versions produce slightly different output.

## CSRF Troubleshooting

If you get CSRF errors when submitting forms, go into `.env` and set:
```
SESSION_COOKIE_SECURE='False'
SESSION_COOKIE_HTTPONLY='False'
```

**Do not use those values in production.**

## JSON-LD contexts

Signature hashing (`jsonld.normalize` in `app/activitypub/signature.py`) needs the ActivityStreams and security/v1 JSON-LD contexts. They are bundled in `app/activitypub/contexts/` and served locally by the custom pyld document loader in `app/jsonld_loader.py`, so w3.org rate limiting (HTTP 429) can't break federation. To refresh the bundled copies from their canonical URLs:

```bash
flask refresh-jsonld-contexts
```
