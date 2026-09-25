//! Core domain policy. The server deliberately does not conceal identity, bypass
//! access controls, solve CAPTCHAs, inspect third-party traffic, or collect biometrics.
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Owner,
    Operator,
    Viewer,
}

impl Role {
    pub fn can_request(&self, action: &Action) -> bool {
        matches!(
            (self, action),
            (Role::Owner, _)
                | (
                    Role::Operator,
                    Action::CreateTask | Action::StartDownload | Action::JoinOwnedRoom
                )
                | (Role::Viewer, Action::ReadStatus)
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    CreateTask,
    StartDownload,
    JoinOwnedRoom,
    ReadStatus,
    ChangeNetworkPolicy,
    ExportData,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApprovalRequest {
    pub id: Uuid,
    pub action: Action,
    pub justification: String,
    pub actor: String,
    pub approved: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NetworkPolicy {
    pub only_user_owned_endpoints: bool,
    pub require_explicit_consent: bool,
    pub max_concurrent_connections: u16,
    pub requests_per_second: u16,
    pub traffic_inspection: bool,
}

impl Default for NetworkPolicy {
    fn default() -> Self {
        Self {
            only_user_owned_endpoints: true,
            require_explicit_consent: true,
            max_concurrent_connections: 4,
            requests_per_second: 2,
            traffic_inspection: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrivacyPolicy {
    pub collect_biometrics: bool,
    pub conceal_identity: bool,
    pub retain_error_logs_days: u16,
    pub permitted_scopes: BTreeSet<String>,
}

impl Default for PrivacyPolicy {
    fn default() -> Self {
        Self {
            collect_biometrics: false,
            conceal_identity: false,
            retain_error_logs_days: 7,
            permitted_scopes: BTreeSet::new(),
        }
    }
}

pub fn create_approval(
    role: Role,
    action: Action,
    actor: String,
    justification: String,
) -> Result<ApprovalRequest, &'static str> {
    if !role.can_request(&action) {
        return Err("role is not permitted to request this action");
    }
    if justification.trim().is_empty() {
        return Err("a human-readable justification is required");
    }
    Ok(ApprovalRequest {
        id: Uuid::new_v4(),
        action,
        actor,
        justification,
        approved: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn viewer_cannot_create_tasks() {
        assert!(!Role::Viewer.can_request(&Action::CreateTask));
    }
    #[test]
    fn approval_requires_reason() {
        assert!(create_approval(Role::Owner, Action::ExportData, "a".into(), " ".into()).is_err());
    }
    #[test]
    fn safe_defaults_disable_sensitive_features() {
        let privacy = PrivacyPolicy::default();
        assert!(!privacy.collect_biometrics && !privacy.conceal_identity);
        assert!(!NetworkPolicy::default().traffic_inspection);
    }
}
