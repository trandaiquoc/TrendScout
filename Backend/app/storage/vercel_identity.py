"""Request-scoped Vercel OIDC federation with service-account impersonation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from time import time

from google.auth import exceptions as auth_exceptions
from google.auth import external_account, impersonated_credentials, jwt
from google.cloud import firestore

from app.settings import Settings
from app.storage.firestore import FirestoreStorage

FIRESTORE_PROJECT_ID = "trendscout-192e4"
WIF_PROVIDER_AUDIENCE = (
    "//iam.googleapis.com/projects/556422620769/locations/global/"
    "workloadIdentityPools/trendscout-vercel/providers/vercel-production"
)
VERCEL_OIDC_ISSUER = "https://oidc.vercel.com/rakoon1"
VERCEL_OIDC_TOKEN_AUDIENCE = "https://vercel.com/rakoon1"
VERCEL_OIDC_SUBJECT = "owner:rakoon1:project:trend-scout:environment:production"

# This is a public service-account identity, not a credential or private key.
VERCEL_IMPERSONATED_SERVICE_ACCOUNT = (
    "trendscout-vercel-backend@trendscout-192e4.iam.gserviceaccount.com"
)
_SERVICE_ACCOUNT_IMPERSONATION_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    f"{VERCEL_IMPERSONATED_SERVICE_ACCOUNT}:generateAccessToken"
)
_MAX_OIDC_TOKEN_LENGTH = 16_384


def _validate_vercel_oidc_token(oidc_token: str, *, now: int | None = None) -> None:
    """Reject tokens whose unverified claims do not match the configured Vercel identity.

    These local checks only reject unexpected claims. Google STS still verifies the
    token signature, issuer, provider audience, and provider attribute condition.
    """
    if (
        not isinstance(oidc_token, str)
        or not oidc_token
        or len(oidc_token) > _MAX_OIDC_TOKEN_LENGTH
        or oidc_token.strip() != oidc_token
    ):
        raise ValueError("Vercel OIDC token is missing or invalid")

    try:
        claims = jwt.decode(oidc_token, verify=False)
    except (auth_exceptions.GoogleAuthError, TypeError, ValueError):
        raise ValueError("Vercel OIDC token is malformed") from None

    if not isinstance(claims, Mapping):
        raise ValueError("Vercel OIDC token has invalid claims")
    if claims.get("iss") != VERCEL_OIDC_ISSUER:
        raise ValueError("Unexpected Vercel OIDC issuer")
    if claims.get("aud") != VERCEL_OIDC_TOKEN_AUDIENCE:
        raise ValueError("Unexpected Vercel OIDC token audience")
    if claims.get("sub") != VERCEL_OIDC_SUBJECT:
        raise ValueError("Vercel OIDC subject is not the authorized production subject")

    expires_at = claims.get("exp")
    current_time = int(time()) if now is None else now
    if type(expires_at) is not int or expires_at <= current_time:
        raise ValueError("Vercel OIDC token is expired or has no valid expiration")


class _VercelSubjectCredentials(external_account.Credentials):
    """External-account source credentials backed by the current request token."""

    def __init__(
        self,
        *,
        oidc_token: str,
        audience: str,
        service_account_impersonation_url=None,
        **credential_options,
    ):
        if audience != WIF_PROVIDER_AUDIENCE:
            raise ValueError("GOOGLE_WIF_AUDIENCE does not match the configured provider")
        _validate_vercel_oidc_token(oidc_token)
        self._oidc_token = oidc_token
        credential_options.setdefault(
            "subject_token_type", "urn:ietf:params:oauth:token-type:jwt"
        )
        credential_options.setdefault("token_url", "https://sts.googleapis.com/v1/token")
        credential_options.setdefault("credential_source", {})
        credential_options.setdefault(
            "scopes", ["https://www.googleapis.com/auth/datastore"]
        )
        super().__init__(
            audience=audience,
            service_account_impersonation_url=service_account_impersonation_url,
            **credential_options,
        )

    def with_scopes(self, scopes, default_scopes=None):
        """Copy source credentials without serializing the request token into config info."""
        scoped = copy(self)
        scoped._scopes = scopes
        scoped._default_scopes = default_scopes
        return scoped

    def retrieve_subject_token(self, request):
        return self._oidc_token


class VercelCredentials(impersonated_credentials.Credentials):
    """Request-scoped Vercel WIF credentials impersonating the reviewed service account."""

    def __init__(self, *, oidc_token: str, audience: str):
        source_credentials = _VercelSubjectCredentials(
            oidc_token=oidc_token,
            audience=audience,
        )
        super().__init__(
            source_credentials=source_credentials,
            target_principal=VERCEL_IMPERSONATED_SERVICE_ACCOUNT,
            target_scopes=["https://www.googleapis.com/auth/datastore"],
            iam_endpoint_override=_SERVICE_ACCOUNT_IMPERSONATION_URL,
        )


def storage_for_request(oidc_token, *, project_id=None, audience=None, settings=None):
    """Construct a request-scoped Firestore reader without any ADC fallback."""
    if settings is None and (project_id is None or audience is None):
        settings = Settings.from_environment()
    project = project_id if project_id is not None else (settings.google_cloud_project or "")
    identity_audience = audience if audience is not None else (settings.google_wif_audience or "")

    if project != FIRESTORE_PROJECT_ID:
        raise ValueError("Vercel Firestore project is missing or not authorized")
    if identity_audience != WIF_PROVIDER_AUDIENCE:
        raise ValueError("Vercel Firestore provider audience is missing or incorrect")

    credentials = VercelCredentials(oidc_token=oidc_token, audience=identity_audience)
    client = firestore.Client(
        project=project,
        database="(default)",
        credentials=credentials,
    )
    return FirestoreStorage(client)
