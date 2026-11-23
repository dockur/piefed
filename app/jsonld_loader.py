# This file is part of PieFed, which is licensed under the GNU Affero General Public License (AGPL) version 3.0.
# You should have received a copy of the GPL along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Custom pyld document loader.
pyld does a GET request during each jsonld.normalize() call to https://www.w3.org/ns/activitystreams which is insane.
It is also sometimes rate limited (HTTP 429) and w3id.org redirects, so relying on live dereferencing makes
signature hashing fail intermittently. This loader serves the most commonly used context from local files and caches
any other remote documents in memory.
"""

import json
import os
import threading
import time

import httpx
from pyld.jsonld import JsonLdError, parse_link_header, LINK_HEADER_REL

CONTEXTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'activitypub', 'contexts')

# Often used contexts that are bundled locally
BUNDLED_CONTEXTS = {
    'https://www.w3.org/ns/activitystreams': 'activitystreams.jsonld',
    'https://w3id.org/security/v1': 'security-v1.jsonld',
    'https://w3c-ccg.github.io/security-vocab/contexts/security-v1.jsonld': 'security-v1.jsonld',
}

CACHE_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3

_cache = {}
_cache_lock = threading.Lock()


def _load_bundled(url):
    filename = BUNDLED_CONTEXTS.get(url)
    if not filename:
        return None
    try:
        with open(os.path.join(CONTEXTS_DIR, filename), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fetch_remote(url, options):
    headers = options.get('headers') or {'Accept': 'application/ld+json, application/json'}
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = httpx.get(url, headers=headers, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
            if response.status_code == 429 or response.status_code >= 500:
                # rate limited or server error: back off and retry
                last_error = f'HTTP {response.status_code}'
                time.sleep(min(2 ** attempt, 8))
                continue
            response.raise_for_status()
            content_type = response.headers.get('content-type', 'application/octet-stream')
            try:
                document = response.json()
            except ValueError as cause:
                raise JsonLdError(
                    'Could not retrieve a JSON-LD document from the URL.',
                    'jsonld.LoadDocumentError',
                    code='loading document failed',
                    cause=cause,
                )
            doc = {
                'contentType': content_type,
                'contextUrl': None,
                'documentUrl': str(response.url),
                'document': document,
            }
            link_header = response.headers.get('link')
            if link_header:
                linked_context = parse_link_header(link_header).get(LINK_HEADER_REL)
                if linked_context and content_type != 'application/ld+json':
                    if isinstance(linked_context, list):
                        raise JsonLdError(
                            'URL could not be dereferenced, it has more than one associated HTTP Link Header.',
                            'jsonld.LoadDocumentError',
                            {'url': url},
                            code='multiple context link headers',
                        )
                    doc['contextUrl'] = linked_context['target']
            return doc
        except JsonLdError:
            raise
        except Exception as cause:
            last_error = str(cause)
            time.sleep(min(2 ** attempt, 8))
    raise JsonLdError(
        'Could not retrieve a JSON-LD document from the URL.',
        'jsonld.LoadDocumentError',
        code='loading document failed',
        cause=Exception(last_error),
    )


def cached_document_loader(url, options=None):
    """bundled contexts first, then cached remote fetches."""
    options = options or {}
    bundled = _load_bundled(url)
    if bundled is not None:
        return {
            'contentType': 'application/ld+json',
            'contextUrl': None,
            'documentUrl': url,
            'document': bundled,
        }
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(url)
        if entry and entry[0] > now:
            return entry[1]
    doc = _fetch_remote(url, options)
    with _cache_lock:
        _cache[url] = (now + CACHE_TTL_SECONDS, doc)
    return doc


class JsonLdLoader:
    """Flask extension that installs the cached loader as pyld's global default document loader."""

    def init_app(self, app):
        from pyld import jsonld
        jsonld.set_document_loader(cached_document_loader)


def download_bundled_contexts():
    """Re-download the bundled context files from their canonical URLs.

    Returns a dict of filename -> source URL for each file written.
    """
    written = {}
    seen_files = set()
    for url, filename in BUNDLED_CONTEXTS.items():
        if filename in seen_files:
            continue  # e.g. the w3id.org redirect target shares a file with its alias
        seen_files.add(filename)
        response = httpx.get(
            url,
            headers={'Accept': 'application/ld+json, application/json'},
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
        document = response.json()  # raises ValueError if the response is not JSON
        if '@context' not in document:
            raise ValueError(f'{url} did not return a JSON-LD context')
        path = os.path.join(CONTEXTS_DIR, filename)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(document, f, indent=2, ensure_ascii=False)
            f.write('\n')
        written[filename] = url
    return written
