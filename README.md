# SecretCrawler 2.0

> GPL-3.0-only · Rust control-plane + Flutter desktop/mobile UI + Python SDK

SecretCrawler 2.0 is a **consent-first, local-by-default automation control plane**. It turns the original crawler direction into an auditable foundation for authorized web tasks, downloads, local integrations, and user-owned virtual-network endpoints.

## Safety and product boundaries

This project does **not** implement stealth or abusive capabilities. In particular, it does not hide device permissions or IP identity, bypass authentication/CAPTCHAs, impersonate users, collect or identify faces/biometrics, intercept third-party traffic, or automatically enter rooms/accounts. These requests require user-visible consent and must only target endpoints the operator owns or is explicitly authorized to manage.

Built-in defaults enforce:

- **Least privilege**: `owner`, `operator`, and `viewer` roles; viewers can only read status.
- **Human approval trail**: sensitive API actions create a reviewable request with an actor and justification; they are never performed implicitly.
- **Private network posture**: user-owned endpoints only, explicit consent, four concurrent connections, two requests per second, and no traffic inspection.
- **Data minimization**: biometric collection and identity-concealment modes are disabled; error-log retention defaults to seven days.

## Repository layout

```text
backend/   Rust/Axum service and policy domain model
frontend/  Flutter Material 3 dashboard
python/    Installable Python client library
```

## Run the Rust backend

```bash
cargo run -p secretcrawler-server
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/policies
```

Set `SECRETCRAWLER_BIND` to change the local listening address. Keep this service behind an authenticated reverse proxy before exposing it beyond a trusted network.

### API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Local service health check |
| `GET` | `/v1/policies` | Read conservative network/privacy defaults |
| `GET` | `/v1/approvals` | List in-memory approval requests |
| `POST` | `/v1/approvals` | Create a justified, reviewable request |

Example request:

```bash
curl -X POST http://127.0.0.1:8080/v1/approvals \
  -H 'content-type: application/json' \
  -d '{"role":"operator","action":"start_download","actor":"alice","justification":"Download a licensed dataset"}'
```

## Run Flutter UI

```bash
cd frontend
flutter pub get
flutter run
```

The dashboard only checks the local backend and documents the current safeguards. It makes no privileged device/network changes.

## Python SDK

```bash
pip install ./python
python - <<'PY'
from secretcrawler_sdk import SecretCrawlerClient
print(SecretCrawlerClient().health())
PY
```

Use `request_approval()` for a reviewable request; it does not execute the requested operation.

## Development

```bash
cargo test --workspace
cargo fmt --all -- --check
cd frontend && flutter analyze
```

## License

SecretCrawler is licensed under [GPL-3.0-only](LICENSE).
