"""Print local configuration status only. No provider requests are made."""

from app.local_credentials import LocalCredentialProvider


def main():
    statuses = LocalCredentialProvider().status()
    for reference, status in statuses.items():
        print(f"{reference}={status}")
    return 0 if all(status == "PRESENT" for status in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
