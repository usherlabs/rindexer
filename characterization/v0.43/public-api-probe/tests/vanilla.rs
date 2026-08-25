use alloy::{
    primitives::{Address, B256, U256, U64},
    rpc::types::ValueOrArray,
};
use rindexer::{
    event::{
        contract_setup::{AddressDetails, IndexingContractSetup},
        RindexerEventFilter,
    },
    manifest::{
        contract::{ContractDetails, EventInputIndexedFilters, FilterDetailsYaml},
        global::Global,
        network::Network,
    },
};

fn alternative(event_name: &str, values: &[&str]) -> EventInputIndexedFilters {
    EventInputIndexedFilters {
        event_name: event_name.to_string(),
        indexed_1: Some(values.iter().map(|value| (*value).to_string()).collect()),
        indexed_2: None,
        indexed_3: None,
    }
}

fn topic_number(value: u64) -> B256 {
    B256::from(U256::from(value))
}

#[test]
fn address_detail_first_alternative_matches_and_later_alternative_is_ignored() {
    let details = AddressDetails {
        address: ValueOrArray::Value(Address::ZERO),
        indexed_filters: Some(vec![
            alternative("Transfer", &["1"]),
            alternative("Transfer", &["2"]),
        ]),
    };

    let filter = RindexerEventFilter::new_address_filter(
        &B256::ZERO,
        "Transfer",
        &details,
        U64::ZERO,
        U64::from(10),
    )
    .expect("address filter should build");

    let topic = filter.topic1();
    assert!(topic.contains(&topic_number(1)));
    assert!(!topic.contains(&topic_number(2)));
}

#[test]
fn address_detail_no_matching_event_has_no_indexed_topic_constraint() {
    let details = AddressDetails {
        address: ValueOrArray::Value(Address::ZERO),
        indexed_filters: Some(vec![alternative("Approval", &["1"])]),
    };

    let filter = RindexerEventFilter::new_address_filter(
        &B256::ZERO,
        "Transfer",
        &details,
        U64::ZERO,
        U64::from(10),
    )
    .expect("address filter should build");

    assert!(filter.topic1().is_empty());
}

#[test]
fn filter_mode_contract_detail_collapses_alternatives_to_the_first_entry() {
    let detail = ContractDetails {
        network: "local".to_string(),
        address: None,
        filter: Some(ValueOrArray::Value(FilterDetailsYaml {
            event_name: "Transfer".to_string(),
        })),
        indexed_filters: Some(vec![
            alternative("Transfer", &["1"]),
            alternative("Transfer", &["2"]),
        ]),
        factory: None,
        start_block: Some(U64::ZERO),
        end_block: Some(U64::from(10)),
    };

    let IndexingContractSetup::Filter(filter_detail) =
        detail.indexing_contract_setup(std::path::Path::new("."))
    else {
        panic!("expected filter-mode setup");
    };
    let filter = RindexerEventFilter::new_filter(
        &B256::ZERO,
        "Transfer",
        &filter_detail,
        U64::ZERO,
        U64::from(10),
    )
    .expect("filter should build");

    assert!(filter.topic1().contains(&topic_number(1)));
    assert!(!filter.topic1().contains(&topic_number(2)));
}

#[test]
fn one_indexed_filter_entry_preserves_every_topic_value() {
    let details = AddressDetails {
        address: ValueOrArray::Value(Address::ZERO),
        indexed_filters: Some(vec![alternative("Swap", &["1", "2", "3"])]),
    };
    let filter = RindexerEventFilter::new_address_filter(
        &B256::ZERO,
        "Swap",
        &details,
        U64::ZERO,
        U64::from(10),
    )
    .expect("address filter should build");

    let topic = filter.topic1();
    assert_eq!(topic.len(), 3);
    for value in 1..=3 {
        assert!(topic.contains(&topic_number(value)));
    }
}

#[test]
fn health_reorg_and_hypersync_defaults_are_observable_through_public_types() {
    assert_eq!(Global::default().health_port, 8080);

    let absent: Network = serde_yaml::from_str(
        "name: local\nchain_id: 31337\nrpc: http://127.0.0.1:8545\n",
    )
    .expect("minimal network should deserialize");
    assert!(absent.reorg_handling.is_none());
    assert!(absent.hypersync.is_none());

    let configured: Network = serde_yaml::from_str(
        "name: local\nchain_id: 31337\nrpc: http://127.0.0.1:8545\nreorg_handling: {}\nhypersync: true\n",
    )
    .expect("configured network should deserialize");
    assert!(configured.reorg_handling.expect("mapping present").enabled);
    assert!(configured.hypersync.is_some());
}
