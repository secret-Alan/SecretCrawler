use secretcrawler_server::{NetworkPolicy, PrivacyPolicy};
#[test]
fn policies_are_conservative_by_default() {
    let network = NetworkPolicy::default();
    let privacy = PrivacyPolicy::default();
    assert!(network.only_user_owned_endpoints && network.require_explicit_consent);
    assert_eq!(network.max_concurrent_connections, 4);
    assert!(!privacy.collect_biometrics);
}
