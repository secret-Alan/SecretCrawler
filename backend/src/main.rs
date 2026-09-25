use axum::{
    extract::State,
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use secretcrawler_server::{
    create_approval, Action, ApprovalRequest, NetworkPolicy, PrivacyPolicy, Role,
};
use serde::{Deserialize, Serialize};
use std::{
    net::SocketAddr,
    sync::{Arc, Mutex},
};
use tower_http::trace::TraceLayer;

#[derive(Clone, Default)]
struct AppState {
    approvals: Arc<Mutex<Vec<ApprovalRequest>>>,
}
#[derive(Serialize)]
struct Health {
    service: &'static str,
    status: &'static str,
}
#[derive(Deserialize)]
struct ApprovalInput {
    role: Role,
    action: Action,
    actor: String,
    justification: String,
}

async fn health() -> Json<Health> {
    Json(Health {
        service: "secretcrawler-server",
        status: "ok",
    })
}
async fn policies() -> Json<(NetworkPolicy, PrivacyPolicy)> {
    Json((NetworkPolicy::default(), PrivacyPolicy::default()))
}
async fn request_approval(
    State(state): State<AppState>,
    Json(input): Json<ApprovalInput>,
) -> Result<(StatusCode, Json<ApprovalRequest>), (StatusCode, String)> {
    let request = create_approval(input.role, input.action, input.actor, input.justification)
        .map_err(|e| (StatusCode::FORBIDDEN, e.into()))?;
    state
        .approvals
        .lock()
        .expect("approval store lock poisoned")
        .push(request.clone());
    Ok((StatusCode::ACCEPTED, Json(request)))
}
async fn list_approvals(State(state): State<AppState>) -> Json<Vec<ApprovalRequest>> {
    Json(
        state
            .approvals
            .lock()
            .expect("approval store lock poisoned")
            .clone(),
    )
}

#[tokio::main]
async fn main() {
    let state = AppState::default();
    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/policies", get(policies))
        .route("/v1/approvals", get(list_approvals).post(request_approval))
        .with_state(state)
        .layer(TraceLayer::new_for_http());
    let address: SocketAddr = std::env::var("SECRETCRAWLER_BIND")
        .unwrap_or_else(|_| "127.0.0.1:8080".into())
        .parse()
        .expect("valid bind address");
    println!("SecretCrawler server listening on http://{address}");
    axum::serve(
        tokio::net::TcpListener::bind(address)
            .await
            .expect("bind listener"),
        app,
    )
    .await
    .expect("serve application");
}
