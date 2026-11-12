# YouTube API audit — upload checklist

**Status: LOCAL DRAFT / NOT YET PUBLIC.** The files below exist as local evidence of the synthetic demo and the proposed integration. They are **not** proof of YouTube approval, live API access, policy compliance, or a publicly accessible privacy policy. The owner has confirmed that no special YouTube permission for derived analysis has been granted. Do not represent synthetic data as live.

Form checked 12 Nov 2026: [YouTube API Services — Audit and Quota Extension Form](https://support.google.com/youtube/contact/yt_api_form?hl=en-GB). Field labels and conditional requirements can change; recheck the signed-in form. The public form text says uploads are simulated and says to upload one file under 10 MB; actual upload behavior and whether that restriction is per field remain unverified. Do not assume a combined upload will work; check each field and seek YouTube support if the form refuses the files.

## Evidence field mapping

`READY (file)` means readable file exists with the requested format, not that it is ready for final submission. `BLOCKED (submission)` means an unresolved factual, publication or compliance item remains.

| Form field | Select this file | Status and disclosure |
| --- | --- | --- |
| Section 5, Project 1 → **Privacy policy screenshots (showing YouTube sections, the Google Privacy Policy link, deletion policies, etc.)** | `01-privacy-policy.png` | READY (file); BLOCKED (submission): local URL and placeholders for identity, contact, effective date, retention and deletion. Update page and recapture after owner review and publication. |
| Section 5, Project 1 → **Homepage screenshot – showing where the Privacy Policy link is located, with YouTube branding visible** | `02-homepage.png` | READY (file); BLOCKED (submission): local URL, draft links and text-only notice that YouTube is pending. No YouTube content is live; check official branding requirements when an approved YouTube feature displays content and recapture the actual state. |
| Section 5, Project 1 → **Terms of Service documentation** | `03-terms-of-service.pdf` | READY (file); BLOCKED (submission): English draft has owner, contact, date and legal placeholders; owner must review and finalize. |
| Section 5, Project 1 → **CONDITIONAL EVIDENCE: Dashboard/Feature screenshots — Internal company tool / Analytics and reporting**, if applicable to the selected category | `04-analytics-dashboard.png` | READY (file); synthetic data and pending YouTube approval are visible. It does not prove a working YouTube analytics feature or live data. This is a personal demo, not an internal company tool. Select the use-case category truthfully. |
| Section 6 → **Architecture diagram** | `05-architecture.pdf` | READY (file) for review: active synthetic execution and proposed guarded YouTube path are distinguished. Update if implementation changes. |
| Section 6 → **User flow diagrams** | `06-user-flow.pdf` | READY (file) for review: anonymous, synthetic local flow; future live path marked pending. Update if implementation changes. |

Each screenshot includes the actual browser address bar at `http://127.0.0.1:8765/...`, rather than an invented public URL. Screenshots are 1600×1000. Each requested file is below 10 MB. The three PDFs have been rendered and reviewed. No provider request or real API key was used to create this package.

## Form sections the owner must complete

1. **Section 1 — Select the reason for your request:** select the actual purpose; do not claim YouTube requested a re-audit unless that happened.
2. **Section 2 — Organisation and contact information:** for this personal project, the form offers **As an individual user** and says to write `self` for organisation legal name. Owner must enter their legal name, contact email, country and address, website and other required details directly in the form. Never include API keys in the form or these attachments.
3. **Section 3 — Organisation profile:** owner must answer business model, distribution, representative and any conditional questions according to facts; no company ownership is implied.
4. **Section 4 — API client overview and access information:** API client name is `TrendScout`; client name does not contain `YouTube`. **Primary access URL**, **Privacy policy URL**, and optional **Terms of Service URL** must be real, publicly accessible HTTPS URLs when required by the form. Current screenshot URLs are loopback only and must not be entered as public URLs. No login or Google OAuth is implemented for the public dashboard; answer access questions truthfully and follow current form instructions for an inaccessible demo or screencast.
5. **Section 5 — Use cases and quota extension details:** owner supplies the correct numeric Google Cloud project number, actual use case, anticipated request volume, `youtube.search.list` quota and justification. Do not invent usage, quota, approval or a current live feature. Select analytics/derived-metrics permission only if applicable and after reviewing the [additional policies](https://developers.google.com/youtube/terms/derived-metrics-policy); a form request does not grant permission. No YouTube-derived processing is approved at present.
6. **Section 6 — Additional evidence and documentation:** attach architecture and flow PDFs in their respective fields. Provide any conditional supporting screencast or materials that the form requires; the files here do not prove a live YouTube feature. The form says certain internal tools and uploading flows require more than static screenshots.
7. **Section 7 — Attestations and submission:** only the owner may read and agree to the [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service), [Developer Policies](https://developers.google.com/youtube/terms/developer-policies), [Google Privacy Policy](https://policies.google.com/privacy), any applicable additional terms, accuracy, data-use and support-recording consents. No agreement or submission was made for the owner.

## Before publishing or submitting

- Replace every placeholder in both policy pages with owner-confirmed facts: legal identity, effective date, contact method, response procedure, actual retention/deletion mechanism, hosting/logs and governing law where applicable. Verify deletion and crash/backup behavior rather than promising an unimplemented process.
- Review privacy-policy acceptance requirements, terms, source attribution/branding and the proposed YouTube use case against current provider policies. A draft policy page and text attribution alone do not establish compliance.
- Publish only after separate approval. Record the actual HTTPS dashboard, policy and terms URLs; confirm Google can access them, then recapture screenshots with the genuine address bar and current feature state.
- Ensure requested additional quota and scope are justified by the actual approved functionality. Verify YouTube permits the proposed handling of API data; retain the current block on derived keyword processing until permission is resolved.
- Do not upload secrets or personal data beyond what the form explicitly requires. Do not claim production readiness, multi-host verification, live provider verification or completed YouTube integration from these offline artifacts.
