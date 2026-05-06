# code in this file was originally from Takahe https://github.com/jointakahe/takahe
#
# Copyright 2022 Andrew Godwin
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation and/or
# other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from typing import Literal, TypedDict, cast
from urllib.parse import urlparse

import pendulum
import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dateutil import parser
from flask import Request, current_app
from furl import furl
from pyld import jsonld
from sqlalchemy import text

from app import db, celery, httpx_client
from app.constants import DATETIME_MS_FORMAT
from app.models import utcnow, ActivityPubLog, Community, Instance, CommunityMember, User, SendQueue
from app.utils import get_task_session


def http_date(epoch_seconds=None):
    if epoch_seconds is None:
        epoch_seconds = pendulum.now('UTC').timestamp()
    return formatdate(epoch_seconds, usegmt=True)  # takahe uses formatdate so let's try that
    # formatted_date = pendulum.from_timestamp(epoch_seconds).format('ddd, DD MMM YYYY HH:mm:ss ZZ', locale='en_US')     # mastodon does not like this
    # return formatted_date


def format_ld_date(value: datetime) -> str:
    # We chop the timestamp to be identical to the timestamps returned by
    # Mastodon's API, because some clients like Toot! (for iOS) are especially
    # picky about timestamp parsing.
    return f"{value.strftime(DATETIME_MS_FORMAT)[:-4]}Z"


def parse_http_date(http_date_str):
    return parsedate_to_datetime(http_date_str)


def parse_ld_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    return parser.isoparse(value).replace(microsecond=0)


def send_post_request(uri: str, body: dict | None, private_key: str, key_id: str,
                      content_type: str = "application/activity+json",
                      method: Literal["get", "post"] = "post", timeout: int = 10, retries: int = 0, new_task=True):
    if current_app.debug or new_task is False:
        return post_request(uri=uri, body=body, private_key=private_key, key_id=key_id, content_type=content_type,
                            method=method, timeout=timeout, retries=retries)
    else:
        post_request.delay(uri=uri, body=body, private_key=private_key, key_id=key_id, content_type=content_type,
                           method=method, timeout=timeout, retries=retries)
        return True


@celery.task
def post_request(uri: str, body: dict | None, private_key: str, key_id: str,
                 content_type: str = "application/activity+json",
                 method: Literal["get", "post"] = "post", timeout: int = 10, retries: int = 0):
    session = get_task_session()
    try:
        if '@context' not in body:  # add a default json-ld context if necessary
            body['@context'] = default_context()
        type = body['type'] if 'type' in body else ''
        log = ActivityPubLog(direction='out', activity_type=type, result='processing', activity_id=body['id'], exception_message='')
        log.activity_json = json.dumps(body)
        session.add(log)

        http_status_code = None

        if uri is None or uri == '':
            log.result = 'failure'
            log.exception_message = 'empty uri'
        else:
            try:
                result = HttpSignature.signed_request(uri, body, private_key, key_id, content_type, method, timeout)
                http_status_code = result.status_code
                if result.status_code != 200 and result.status_code != 202 and result.status_code != 204:
                    log.result = 'failure'
                    log.exception_message = f'{result.status_code}: {result.text:.100}' + ' - '
                    if 'DOCTYPE html' in result.text:
                        log.result = 'ignored'
                        log.exception_message = f'{result.status_code}: HTML instead of JSON response'
                        # log.activity_json += result.text[]
                    elif 'community_has_no_followers' in result.text:
                        fix_local_community_membership(uri, private_key, session)
                    elif result.status_code == 400 and 'person_is_banned_from_site' in result.text:
                        from app.activitypub.util import process_banned_message
                        process_banned_message(result.json(), furl(uri).host, session)
                    elif result.status_code == 410 or result.status_code == 418:    # When an instance returns 410, never send to it again.
                        existing_instance = session.query(Instance).filter_by(domain=furl(uri).host).first()
                        if existing_instance:
                            existing_instance.gone_forever = True
                        session.query(SendQueue).filter_by(destination_domain=furl(uri).host).delete()
                    else:
                        if current_app.debug:
                            current_app.logger.error(f'Response code for post attempt to {uri} was ' +
                                                     str(result.status_code) + ' ' + result.text[:50])
                log.exception_message += uri
                if result.status_code == 202:
                    log.exception_message += ' 202'
                if result.status_code == 204:
                    log.exception_message += ' 204'
                result.close()
            except Exception as e:
                log.result = 'failure'
                log.exception_message = 'could not send:' + str(e)
                if current_app.debug:
                    current_app.logger.error(f'Exception while sending post to {uri}')
                http_status_code = 404
        if log.result == 'processing':
            log.result = 'success'
        session.commit()

        if log.result != 'failure':
            return
        else:
            if http_status_code is not None and (http_status_code == 429 or http_status_code >= 500):
                if content_type == "application/activity+json":
                    # Calculate retry delay with exponential backoff. 1 min, 2 mins, 4 mins, 8 mins, up to 4h
                    backoff = 60 * (2 ** retries)
                    backoff = min(backoff, 15360)
                    session.add(SendQueue(destination=uri, destination_domain=furl(uri).host, actor=key_id,
                                             private_key=private_key, payload=json.dumps(body), retries=retries,
                                             retry_reason=log.exception_message,
                                             send_after=datetime.utcnow() + timedelta(seconds=backoff)))
                    session.commit()

            return
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def signed_get_request(uri: str, private_key: str, key_id: str, content_type: str = "application/activity+json",
                       method: Literal["get", "post"] = "get", timeout: int = 10, ):
    result = HttpSignature.signed_request(uri, None, private_key, key_id, content_type, method, timeout)
    return result


class VerificationError(BaseException):
    """
    There was an error with verifying the signature
    """

    pass


class VerificationFormatError(VerificationError):
    """
    There was an error with the format of the signature (not if it is valid)
    """

    pass


class RsaKeys:
    @classmethod
    def generate_keypair(cls) -> tuple[str, str]:
        """
        Generates a new RSA keypair
        """
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        private_key_serialized = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        public_key_serialized = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        return private_key_serialized, public_key_serialized


# Get a piece of the signature string. Similar to parse_signature except unencumbered by needing to return a HttpSignatureDetails
def signature_part(signature, key):
    parts = signature.split(',')
    for part in parts:
        part_parts = part.split('=')
        part_parts[0] = part_parts[0].strip()
        if part_parts[0] == key:
            return part_parts[1].strip().replace('"', '')
    return ''


class HttpSignature:
    """
    Allows for calculation and verification of HTTP signatures
    """

    # Class-level cache for deserialized private keys
    _private_key_cache = {}  # key: hash of private_key, value: RSAPrivateKey instance
    _public_key_cache = {}  # key: hash of public_key, value: RSAPublicKey instance
    _cache_max_size = 1000  # Limit to prevent unbounded growth

    @classmethod
    def _get_private_key_instance(cls, private_key: str) -> rsa.RSAPrivateKey:
        """
        Get a deserialized private key instance from cache, or load and cache it.
        Uses SHA-256 hash of the private key as the cache key to avoid storing large strings.
        """
        # Use hash of private key as cache key (shorter than full PEM string)
        key_hash = hashlib.sha256(private_key.encode()).hexdigest()

        if key_hash not in cls._private_key_cache:
            # Simple size limit: clear oldest half when full
            if len(cls._private_key_cache) >= cls._cache_max_size:
                # Remove first half
                keys_to_remove = list(cls._private_key_cache.keys())[:cls._cache_max_size // 2]
                for k in keys_to_remove:
                    del cls._private_key_cache[k]

            # Deserialize and cache
            cls._private_key_cache[key_hash] = cast(
                rsa.RSAPrivateKey,
                serialization.load_pem_private_key(
                    private_key.encode("ascii"),
                    password=None,
                ),
            )

        return cls._private_key_cache[key_hash]

    @classmethod
    def _get_public_key_instance(cls, public_key: str) -> rsa.RSAPublicKey:
        """
        Get a deserialized public key instance from cache, or load and cache it.
        Uses SHA-256 hash of the public key as the cache key to avoid storing large strings.
        """
        # Use hash of public key as cache key (shorter than full PEM string)
        key_hash = hashlib.sha256(public_key.encode()).hexdigest()

        if key_hash not in cls._public_key_cache:
            # Simple size limit: clear oldest half when full
            if len(cls._public_key_cache) >= cls._cache_max_size:
                # Remove first half
                keys_to_remove = list(cls._public_key_cache.keys())[:cls._cache_max_size // 2]
                for k in keys_to_remove:
                    del cls._public_key_cache[k]

            # Deserialize and cache
            cls._public_key_cache[key_hash] = cast(
                rsa.RSAPublicKey,
                serialization.load_pem_public_key(public_key.encode("ascii")),
            )

        return cls._public_key_cache[key_hash]

    @classmethod
    def calculate_digest(cls, data, algorithm="sha-256") -> str:
        """
        Calculates the digest header value for a given HTTP body
        """
        if algorithm == "sha-256":
            digest = hashes.Hash(hashes.SHA256())
            digest.update(data)
            return "SHA-256=" + base64.b64encode(digest.finalize()).decode("ascii")
        else:
            raise ValueError(f"Unknown digest algorithm {algorithm}")

    @classmethod
    def headers_from_request(cls, request: Request, header_names: list[str]) -> str:
        """
        Creates the to-be-signed header payload from a Flask request
        """
        headers = {}
        for header_name in header_names:
            if header_name == "(request-target)":
                value = f"{request.method.lower()} {request.path}"
            elif header_name == '(created)':
                value = signature_part(request.headers.get('Signature'), 'created')  # Don't use parse_signature because changing HttpSignatureDetails changes everything & I don't have the spoons for that ATM.
            elif header_name == '(expires)':
                value = signature_part(request.headers.get('Signature'), 'expires')
            elif header_name == "content-type":
                value = request.headers.get("Content-Type", "")
            elif header_name == "content-length":
                value = request.headers.get("Content-Length", "")
            else:
                value = request.headers.get(header_name.replace("-", "_").upper(), "")
            headers[header_name] = value
        return "\n".join(f"{name.lower()}: {value}" for name, value in headers.items())

    @classmethod
    def parse_signature(cls, signature: str) -> "HttpSignatureDetails":
        bits = {}
        for item in signature.split(","):
            name, value = item.split("=", 1)
            value = value.strip('"')
            bits[name.lower()] = value
        try:
            signature_details: HttpSignatureDetails = {
                "headers": bits["headers"].split(),
                "signature": base64.b64decode(bits["signature"]),
                "algorithm": bits["algorithm"],
                "keyid": bits["keyid"],
            }
        except KeyError as e:
            key_names = " ".join(bits.keys())
            raise VerificationError(
                f"Missing item from details (have: {key_names}, error: {e})"
            )
        return signature_details

    @classmethod
    def compile_signature(cls, details: "HttpSignatureDetails") -> str:
        value = f'keyId="{details["keyid"]}",headers="'
        value += " ".join(h.lower() for h in details["headers"])
        value += '",signature="'
        value += base64.b64encode(details["signature"]).decode("ascii")
        value += f'",algorithm="{details["algorithm"]}"'
        return value

    @classmethod
    def verify_signature(
            cls,
            signature: bytes,
            cleartext: str,
            public_key: str,
    ):
        public_key_instance = cls._get_public_key_instance(public_key)
        try:
            public_key_instance.verify(
                signature,
                cleartext.encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            raise VerificationError("Signature mismatch")

    @classmethod
    def precheck(cls, request: Request):
        """
        Checks digest and date headers before signature verification.
        Digest is required and must match the body. Date must be within 1 hour.
        """
        if "digest" not in request.headers:
            raise VerificationFormatError("No digest header present")
        expected_digest = HttpSignature.calculate_digest(request.data)
        if request.headers["digest"] != expected_digest:
            raise VerificationFormatError("Digest is incorrect")

        if "date" not in request.headers:
            raise VerificationFormatError("No date header present")
        header_date = parse_http_date(request.headers["date"])
        if abs((datetime.now(timezone.utc) - header_date).total_seconds()) > 3600:
            raise VerificationFormatError("Date is too far away")

    @classmethod
    def verify_request(cls, request: Request, public_key, skip_date=False):
        """
        Verifies that the request has a valid signature for its body
        """
        # Verify body digest
        if "digest" in request.headers:
            expected_digest = HttpSignature.calculate_digest(request.data)
            if request.headers["digest"] != expected_digest:
                raise VerificationFormatError("Digest is incorrect")

        # Verify date header
        if "date" in request.headers and not skip_date:
            header_date = parse_http_date(request.headers["date"])
            if abs((datetime.now(timezone.utc) - header_date).total_seconds()) > 3600:
                raise VerificationFormatError("Date is too far away")

        # Get the signature details
        if "signature" not in request.headers:
            raise VerificationFormatError("No signature header present")
        signature_details = cls.parse_signature(request.headers["signature"])

        # Reject unknown algorithms
        # hs2019 is used by some libraries to obfuscate the real algorithm per the spec
        # https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures-12
        if (
                signature_details["algorithm"] != "rsa-sha256"
                and signature_details["algorithm"] != "hs2019"
        ):
            raise VerificationFormatError("Unknown signature algorithm")
        # Create the signature payload
        headers_string = cls.headers_from_request(request, signature_details["headers"])
        cls.verify_signature(
            signature_details["signature"],
            headers_string,
            public_key,
        )
        return True

    @classmethod
    def signed_request(
            cls,
            uri: str,
            body: dict | None,
            private_key: str,
            key_id: str,
            content_type: str = "application/activity+json",
            method: Literal["get", "post"] = "post",
            timeout: int = 5,
            send_via_async=False
    ):
        """
        Performs a request to the given path, with a document, signed
        as an identity.
        """
        if "://" not in uri:
            raise ValueError("URI does not contain a scheme")
        # Create the core header field set
        uri_parts = urlparse(uri)
        date_string = http_date()
        headers = {
            "(request-target)": f"{method} {uri_parts.path}",
            "Host": uri_parts.hostname,
            "Date": date_string,
        }
        # If we have a body, add a digest and content type
        if body is not None:
            if '@context' not in body:  # add a default json-ld context if necessary
                body['@context'] = default_context()
            body_bytes = json.dumps(body).encode("utf8")
            headers["Digest"] = cls.calculate_digest(body_bytes)
            headers["Content-Type"] = content_type
        else:
            body_bytes = b""
        # GET requests get implicit accept headers added
        if method == "get":
            headers["Accept"] = "application/ld+json"
        # Sign the headers
        signed_string = "\n".join(
            f"{name.lower()}: {value}" for name, value in headers.items()
        )

        private_key_instance = cls._get_private_key_instance(private_key)

        # RSA signature generation
        signature = private_key_instance.sign(
            signed_string.encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        headers["Signature"] = cls.compile_signature(
            {
                "keyid": key_id,
                "headers": list(headers.keys()),
                "signature": signature,
                "algorithm": "rsa-sha256",
            }
        )

        headers["User-Agent"] = f'PieFed/{current_app.config["VERSION"]}; +https://{current_app.config["SERVER_NAME"]}'

        # Send the request with all those headers except the pseudo one
        del headers["(request-target)"]
        if send_via_async:  # 'async' sending involves passing the data through to the piefed_notifs service. See announce_activity_to_followers().
            return uri, headers, body_bytes
        else:
            try:
                response = httpx_client.request(
                    method,
                    uri,
                    headers=headers,
                    data=body_bytes,
                    timeout=timeout,
                    follow_redirects=method == "GET",
                )
            except httpx.HTTPError as ex:
                # Convert to a more generic error we handle
                raise httpx.HTTPError(f"HTTP Exception for {ex.request.url} - {ex}") from None

            if (
                    method == "POST"
                    and 400 <= response.status_code < 500
                    and response.status_code != 404
            ):
                raise ValueError(
                    f"POST error to {uri}: {response.status_code} {response.content!r}"
                )

            return response


class HttpSignatureDetails(TypedDict):
    algorithm: str
    headers: list[str]
    signature: bytes
    keyid: str


class LDSignature:
    """
    Creates and verifies signatures of JSON-LD documents
    """

    @classmethod
    def verify_signature(cls, document: dict, public_key: str) -> None:
        """
        Verifies a document
        """
        try:
            # Strip out the signature from the incoming document
            signature = document.pop("signature")
            # Create the options document
            options = {
                "@context": "https://w3id.org/security/v1",
                "creator": signature["creator"],
                "created": signature["created"],
            }
        except KeyError:
            raise VerificationFormatError("Invalid signature section")
        if signature["type"].lower() != "rsasignature2017":
            raise VerificationFormatError("Unknown signature type")
        # Get the normalised hash of each document
        final_hash = cls.normalized_hash(options) + cls.normalized_hash(document)
        # Verify the signature
        public_key_instance: rsa.RSAPublicKey = cast(
            rsa.RSAPublicKey,
            serialization.load_pem_public_key(public_key.encode("ascii")),
        )
        try:
            public_key_instance.verify(
                base64.b64decode(signature["signatureValue"]),
                final_hash,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            raise VerificationError("Signature mismatch")

    @classmethod
    def create_signature(
            cls, document: dict, private_key: str, key_id: str
    ) -> dict[str, str]:
        """
        Creates the signature for a document
        """
        # Create the options document
        options: dict[str, str] = {
            "@context": "https://w3id.org/security/v1",
            "creator": key_id,
            "created": format_ld_date(utcnow()),
        }
        # Get the normalised hash of each document
        final_hash = cls.normalized_hash(options) + cls.normalized_hash(document)
        # Create the signature
        private_key_instance: rsa.RSAPrivateKey = cast(
            rsa.RSAPrivateKey,
            serialization.load_pem_private_key(
                private_key.encode("ascii"),
                password=None,
            ),
        )
        signature = base64.b64encode(
            private_key_instance.sign(
                final_hash,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        )
        # Add it to the options document along with other bits
        options["signatureValue"] = signature.decode("ascii")
        options["type"] = "RsaSignature2017"
        return options

    @classmethod
    def normalized_hash(cls, document) -> bytes:
        """
        Takes a JSON-LD document and create a hash of its URDNA2015 form,
        in the same way that Mastodon does internally.

        Reference: https://socialhub.activitypub.rocks/t/making-sense-of-rsasignature2017/347
        """
        norm_form = jsonld.normalize(
            document,
            {"algorithm": "URDNA2015", "format": "application/n-quads"},
        )
        digest = hashes.Hash(hashes.SHA256())
        digest.update(norm_form.encode("utf8"))
        return digest.finalize().hex().encode("ascii")


def default_context():
    context = [
        "https://www.w3.org/ns/activitystreams",
        "https://w3id.org/security/v1",
    ]
    if current_app.config['FULL_AP_CONTEXT']:
        context.append({
            "lemmy": "https://join-lemmy.org/ns#",
            "litepub": "http://litepub.social/ns#",
            "pt": "https://joinpeertube.org/ns#",
            "sc": "http://schema.org/",
            "ChatMessage": "litepub:ChatMessage",
            "commentsEnabled": "pt:commentsEnabled",
            "sensitive": "as:sensitive",
            "matrixUserId": "lemmy:matrixUserId",
            "postingRestrictedToMods": "lemmy:postingRestrictedToMods",
            "removeData": "lemmy:removeData",
            "stickied": "lemmy:stickied",
            "moderators": {
                "@type": "@id",
                "@id": "lemmy:moderators"
            },
            "expires": "as:endTime",
            "distinguished": "lemmy:distinguished",
            "language": "sc:inLanguage",
            "identifier": "sc:identifier"
        })
    return context


def fix_local_community_membership(uri: str, private_key: str, session):
    community = session.query(Community).filter_by(private_key=private_key).first()
    parsed_url = urlparse(uri)
    instance_domain = parsed_url.netloc
    instance = session.query(Instance).filter_by(domain=instance_domain).first()

    if community and instance:
        followers = session.query(CommunityMember).filter_by(community_id=community.id). \
            join(User, User.id == CommunityMember.user_id). \
            filter(User.instance_id == instance.id)
        for f in followers:
            session.execute(
                text('DELETE FROM "community_member" WHERE user_id = :user_id AND community_id = :community_id'),
                {'user_id': f.user_id, 'community_id': community.id})
