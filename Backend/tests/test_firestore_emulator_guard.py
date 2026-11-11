"""Safety contract for the explicit synthetic-project Firestore emulator client."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from google.auth.credentials import AnonymousCredentials

from app.storage.firestore_emulator import (
    EmulatorConfigurationError,
    GuardedFirestoreEmulatorClient,
    create_guarded_emulator_client,
)
from app.jobs.firestore_transactional import FirestoreTransactionalCoordinator


PROJECT = "demo-trendscout-p3s1"
HOST = "127.0.0.1:8080"


class FirestoreEmulatorGuardTests(TestCase):
    def environment(self, **overrides):
        values = {
            "FIRESTORE_EMULATOR_HOST": HOST,
            "TREND_SCOUT_EMULATOR_PROJECT_ID": PROJECT,
        }
        values.update(overrides)
        return patch.dict("os.environ", values, clear=True)

    def test_missing_host_fails_before_socket_or_client(self):
        with self.environment(FIRESTORE_EMULATOR_HOST=""):
            with patch("app.storage.firestore_emulator.http.client.HTTPConnection") as probe:
                with patch("app.storage.firestore_emulator.firestore.Client") as client:
                    with self.assertRaises(EmulatorConfigurationError):
                        create_guarded_emulator_client()
            probe.assert_not_called()
            client.assert_not_called()

    def test_non_loopback_host_is_rejected_without_resolution(self):
        for host in ("firestore.googleapis.com:443", "192.0.2.10:8080", "localhost:8080", "http://127.0.0.1:8080"):
            with self.subTest(host=host), self.environment(FIRESTORE_EMULATOR_HOST=host):
                with patch("app.storage.firestore_emulator.http.client.HTTPConnection") as probe:
                    with self.assertRaises(EmulatorConfigurationError):
                        create_guarded_emulator_client()
                probe.assert_not_called()

    def test_real_firebase_project_id_is_rejected(self):
        with self.environment(TREND_SCOUT_EMULATOR_PROJECT_ID="trendscout-192e4"):
            with patch("app.storage.firestore_emulator.http.client.HTTPConnection") as probe:
                with self.assertRaises(EmulatorConfigurationError):
                    create_guarded_emulator_client()
            probe.assert_not_called()

    def test_adc_configuration_is_rejected(self):
        with self.environment(GOOGLE_APPLICATION_CREDENTIALS="/path/must-not-be-read.json"):
            with patch("app.storage.firestore_emulator.http.client.HTTPConnection") as probe:
                with self.assertRaises(EmulatorConfigurationError):
                    create_guarded_emulator_client()
            probe.assert_not_called()

    def test_production_project_environment_is_rejected(self):
        with self.environment(GOOGLE_CLOUD_PROJECT="trendscout-192e4"):
            with patch("app.storage.firestore_emulator.http.client.HTTPConnection") as probe:
                with self.assertRaises(EmulatorConfigurationError):
                    create_guarded_emulator_client()
            probe.assert_not_called()

    def test_host_network_namespace_is_rejected_before_emulator_probe(self):
        with self.environment():
            with patch(
                "app.storage.firestore_emulator._assert_network_namespace_isolated",
                side_effect=EmulatorConfigurationError("not isolated"),
            ):
                with patch("app.storage.firestore_emulator.http.client.HTTPConnection") as probe:
                    with self.assertRaises(EmulatorConfigurationError):
                        create_guarded_emulator_client()
            probe.assert_not_called()

    def test_unreachable_emulator_fails_closed_before_sdk_client_creation(self):
        with self.environment():
            connection = SimpleNamespace(
                request=lambda *args, **kwargs: None,
                getresponse=lambda: (_ for _ in ()).throw(ConnectionRefusedError("closed")),
                close=lambda: None,
            )
            with patch(
                "app.storage.firestore_emulator.http.client.HTTPConnection",
                return_value=connection,
            ) as probe:
                with patch("app.storage.firestore_emulator.firestore.Client") as client:
                    with patch("app.storage.firestore_emulator._assert_network_namespace_isolated"):
                        with self.assertRaises(EmulatorConfigurationError):
                            create_guarded_emulator_client()
            probe.assert_called_once_with("127.0.0.1", 8080, timeout=1.0)
            client.assert_not_called()

    def test_invalid_emulator_protocol_response_fails_closed(self):
        with self.environment():
            response = SimpleNamespace(
                status=200,
                getheader=lambda name: "text/html",
                read=lambda: b"<html>not Firestore</html>",
            )
            connection = SimpleNamespace(
                request=lambda *args, **kwargs: None,
                getresponse=lambda: response,
                close=lambda: None,
            )
            with patch(
                "app.storage.firestore_emulator.http.client.HTTPConnection",
                return_value=connection,
            ):
                with patch("app.storage.firestore_emulator._assert_network_namespace_isolated"):
                    with self.assertRaises(EmulatorConfigurationError):
                        create_guarded_emulator_client()

    def test_client_is_explicit_project_and_anonymous_no_adc(self):
        with self.environment():
            response = SimpleNamespace(
                status=200,
                getheader=lambda name: "application/json" if name == "Content-Type" else None,
                read=lambda: b'{"documents": []}',
            )
            connection = SimpleNamespace(
                request=lambda *args, **kwargs: None,
                getresponse=lambda: response,
                close=lambda: None,
            )
            with patch(
                "app.storage.firestore_emulator.http.client.HTTPConnection",
                return_value=connection,
            ) as probe:
                with patch("app.storage.firestore_emulator._assert_network_namespace_isolated"):
                    with patch("app.storage.firestore_emulator.firestore.Client") as client:
                        client.return_value = object()
                        result = create_guarded_emulator_client()
            self.assertIsInstance(result, GuardedFirestoreEmulatorClient)
            self.assertIs(result._client, client.return_value)
            kwargs = client.call_args.kwargs
            self.assertEqual(kwargs["project"], PROJECT)
            self.assertEqual(kwargs["database"], "(default)")
            self.assertIsInstance(kwargs["credentials"], AnonymousCredentials)
            probe.assert_called_once_with("127.0.0.1", 8080, timeout=1.0)

    def test_transactional_adapter_rejects_unmarked_sdk_client(self):
        with self.assertRaisesRegex(ValueError, "guarded synthetic Firestore Emulator client"):
            FirestoreTransactionalCoordinator(object())


if __name__ == "__main__":
    import unittest

    unittest.main()
