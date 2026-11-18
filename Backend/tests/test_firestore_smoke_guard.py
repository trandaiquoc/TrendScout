import asyncio
from unittest import TestCase
from unittest.mock import patch

from scripts.firestore_smoke import _validate_target, main


class FirestoreSmokeGuardTests(TestCase):
    def test_production_project_is_rejected_even_with_confirmation(self):
        for production_identifier in ("trendscout-192e4", "556422620769"):
            with self.subTest(project=production_identifier):
                with self.assertRaisesRegex(SystemExit, "production project"):
                    _validate_target(
                        production_identifier,
                        production_identifier,
                        acknowledge_nonproduction_writes=True,
                        emulator_host=None,
                    )

    def test_project_id_with_surrounding_whitespace_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "without surrounding whitespace"):
            _validate_target(
                " approved-staging ",
                " approved-staging ",
                acknowledge_nonproduction_writes=True,
                emulator_host=None,
            )

    def test_mismatched_project_confirmation_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "does not match"):
            _validate_target(
                "approved-staging",
                "different-project",
                acknowledge_nonproduction_writes=True,
                emulator_host=None,
            )

    def test_emulator_override_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "Emulator override"):
            _validate_target(
                "approved-staging",
                "approved-staging",
                acknowledge_nonproduction_writes=True,
                emulator_host="127.0.0.1:8080",
            )

    def test_nonproduction_writes_require_explicit_acknowledgement(self):
        with self.assertRaisesRegex(SystemExit, "acknowledge-nonproduction-writes"):
            _validate_target(
                "approved-staging",
                "approved-staging",
                acknowledge_nonproduction_writes=False,
                emulator_host=None,
            )

    def test_main_refuses_production_before_initializing_admin_sdk(self):
        environment = {
            "GOOGLE_CLOUD_PROJECT": "trendscout-192e4",
            "FIRESTORE_EMULATOR_HOST": "",
        }
        arguments = [
            "firestore_smoke",
            "--confirm-nonproduction-project",
            "trendscout-192e4",
            "--acknowledge-nonproduction-writes",
        ]
        with patch.dict("os.environ", environment, clear=True), patch(
            "sys.argv", arguments
        ), patch("scripts.firestore_smoke.FirestoreStorage.from_project") as from_project:
            with self.assertRaisesRegex(SystemExit, "production project"):
                asyncio.run(main())
        from_project.assert_not_called()

    def test_explicitly_acknowledged_nonproduction_target_is_accepted(self):
        _validate_target(
            "approved-staging",
            "approved-staging",
            acknowledge_nonproduction_writes=True,
            emulator_host=None,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
