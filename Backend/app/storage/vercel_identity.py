"""Request-scoped Vercel OIDC federation; never falls back to local ADC."""
from google.auth import external_account
from google.cloud import firestore

from app.settings import Settings
from app.storage.firestore import FirestoreStorage


class VercelCredentials(external_account.Credentials):
    def __init__(self, *, oidc_token, audience):
        self._oidc_token = oidc_token
        super().__init__(
            audience=audience,
            subject_token_type="urn:ietf:params:oauth:token-type:jwt",
            token_url="https://sts.googleapis.com/v1/token",
            credential_source={},
            scopes=["https://www.googleapis.com/auth/datastore"],
        )

    def retrieve_subject_token(self, request):
        return self._oidc_token


def storage_for_request(oidc_token, *, project_id=None, audience=None, settings=None):
    if settings is None and (project_id is None or audience is None):
        settings = Settings.from_environment()
    project = project_id if project_id is not None else (settings.google_cloud_project or "")
    identity_audience = audience if audience is not None else (settings.google_wif_audience or "")
    if not oidc_token or not project or not identity_audience.startswith("//iam.googleapis.com/projects/"):
        raise ValueError("Read-only Firestore identity is not configured")
    credentials = VercelCredentials(oidc_token=oidc_token, audience=identity_audience)
    return FirestoreStorage(firestore.Client(project=project, database="(default)", credentials=credentials))
