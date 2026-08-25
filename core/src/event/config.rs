use alloy::json_abi::Event;
use alloy::primitives::{keccak256, Address, B256, U64};
use alloy::rpc::types::ValueOrArray;
use std::collections::HashMap;
use std::{path::PathBuf, sync::Arc};
use tokio::sync::broadcast::Sender;
use tokio_util::sync::CancellationToken;

use crate::database::clickhouse::client::ClickhouseClient;
use crate::event::contract_setup::{AddressDetails, IndexingContractSetup};
use crate::event::factory_event_filter_sync::update_known_factory_deployed_addresses;
use crate::event::rindexer_event_filter::FactoryFilter;
use crate::indexer::reorg::ReorgEvent;
use crate::indexer::tables::TableRuntime;
use crate::manifest::config::Config;
use crate::manifest::contract::EventInputIndexedFilters;
use crate::manifest::core::Constants;
use crate::provider::ChainProvider;
use crate::streams::StreamsClients;
use crate::types::single_or_array::StringOrArray;
use crate::{
    event::{
        callback_registry::{
            EventCallbackRegistry, EventResult, TraceCallbackRegistry, TraceResult,
        },
        contract_setup::NetworkContract,
        BuildRindexerFilterError, RindexerEventFilter,
    },
    indexer::IndexingEventsProgressState,
    manifest::{native_transfer::TraceProcessingMethod, storage::CsvDetails},
    PostgresClient,
};

pub struct ContractEventProcessingConfig {
    pub id: String,
    pub project_path: PathBuf,
    pub indexer_name: String,
    pub contract_name: String,
    pub topic_id: B256,
    pub event_name: String,
    pub config: Config,
    pub network_contract: Arc<NetworkContract>,
    pub timestamps: bool,
    pub start_block: U64,
    pub end_block: U64,
    pub registry: Arc<EventCallbackRegistry>,
    pub progress: Arc<IndexingEventsProgressState>,
    pub postgres: Option<Arc<PostgresClient>>,
    pub clickhouse: Option<Arc<ClickhouseClient>>,
    pub csv_details: Option<CsvDetails>,
    pub stream_last_synced_block_file_path: Option<String>,
    pub index_event_in_order: bool,
    pub live_indexing: bool,
    pub indexing_distance_from_head: U64,
    /// Per-generation cancellation token for hot-reload support.
    pub cancel_token: CancellationToken,
    /// Derived/custom tables associated with this event (for reorg cleanup).
    pub tables: Arc<Vec<TableRuntime>>,
    /// Broadcast sender for reorg events (code-gen mode).
    pub reorg_sender: Option<Sender<ReorgEvent>>,
    /// Streams clients for reorg retraction (no-code mode).
    pub streams_clients: Arc<Option<StreamsClients>>,
    /// ABI path(s) for reconstructing raw events during replay.
    pub contract_abi: Option<StringOrArray>,
    /// Providers by network for replay parity with no-code table execution.
    pub providers: Arc<HashMap<String, Arc<dyn ChainProvider>>>,
    /// Constants for replay parity with no-code table execution.
    pub constants: Arc<Constants>,
    /// Multicall overrides by network for replay parity.
    pub multicall_addresses: Arc<HashMap<String, Option<String>>>,
}

// FIET-PATCH:outerlook-85b7dad-per-detail-cursors
pub const LEGACY_DETAIL_KEY: &str = "__event__";

/// Derive the durable cursor identity for one manifest detail.
///
/// Address-scoped keys retain the historical FIET byte format. Filter-mode
/// details use an explicit prefix because they have no contract address.
pub fn derive_detail_key(
    indexing_contract_setup: &IndexingContractSetup,
    event_name: &str,
) -> String {
    fn indexed_positions(indexed_filter: &EventInputIndexedFilters) -> Vec<String> {
        [
            (1, indexed_filter.indexed_1.as_ref()),
            (2, indexed_filter.indexed_2.as_ref()),
            (3, indexed_filter.indexed_3.as_ref()),
        ]
        .into_iter()
        .filter_map(|(position, values)| {
            values
                .filter(|values| !values.is_empty())
                .map(|values| format!("i{position}:{}", values.join(",")))
        })
        .collect()
    }

    let (prefix, indexed_filters): (String, Vec<&EventInputIndexedFilters>) =
        match indexing_contract_setup {
            IndexingContractSetup::Address(details) => {
                let address = match &details.address {
                    ValueOrArray::Value(address) => address.to_string(),
                    ValueOrArray::Array(addresses) if !addresses.is_empty() => {
                        addresses.iter().map(ToString::to_string).collect::<Vec<_>>().join(",")
                    }
                    ValueOrArray::Array(_) => return LEGACY_DETAIL_KEY.to_string(),
                };
                let filter = details
                    .indexed_filters
                    .as_ref()
                    .map(|filters| {
                        filters.iter().filter(|filter| filter.event_name == event_name).collect()
                    })
                    .unwrap_or_default();
                (address, filter)
            }
            IndexingContractSetup::Factory(details) => {
                let address = match &details.address {
                    ValueOrArray::Value(address) => address.to_string(),
                    ValueOrArray::Array(addresses) if !addresses.is_empty() => {
                        addresses.iter().map(ToString::to_string).collect::<Vec<_>>().join(",")
                    }
                    ValueOrArray::Array(_) => return LEGACY_DETAIL_KEY.to_string(),
                };
                let filter = details
                    .indexed_filters
                    .as_ref()
                    .map(|filters| {
                        filters.iter().filter(|filter| filter.event_name == event_name).collect()
                    })
                    .unwrap_or_default();
                (address, filter)
            }
            IndexingContractSetup::Filter(details) => {
                let filter = details
                    .indexed_filters
                    .as_ref()
                    .map(|filters| {
                        filters.iter().filter(|filter| filter.event_name == event_name).collect()
                    })
                    .unwrap_or_default();
                (format!("filter:{event_name}"), filter)
            }
        };

    if indexed_filters.is_empty() {
        return LEGACY_DETAIL_KEY.to_string();
    }

    let mut alternatives = Vec::with_capacity(indexed_filters.len());
    for indexed_filter in indexed_filters {
        let positions = indexed_positions(indexed_filter);
        if positions.is_empty() {
            return LEGACY_DETAIL_KEY.to_string();
        }
        alternatives.push(positions.join(":"));
    }

    format!("{prefix}:{}", alternatives.join("|")).to_ascii_lowercase()
}

impl ContractEventProcessingConfig {
    pub fn info_log_name(&self) -> String {
        format!("{}::{}::{}", self.contract_name, self.event_name, self.network_contract.network)
    }

    pub fn to_event_filter(&self) -> Result<RindexerEventFilter, BuildRindexerFilterError> {
        match &self.network_contract.indexing_contract_setup {
            IndexingContractSetup::Address(details) => RindexerEventFilter::new_address_filter(
                &self.topic_id,
                &self.event_name,
                details,
                self.start_block,
                self.end_block,
            ),
            IndexingContractSetup::Filter(details) => RindexerEventFilter::new_filter(
                &self.topic_id,
                &self.event_name,
                details,
                self.start_block,
                self.end_block,
            ),
            IndexingContractSetup::Factory(details) => {
                let index_filter = details.indexed_filters.iter().find_map(|indexed_filters| {
                    indexed_filters.iter().find(|&n| n.event_name == self.event_name)
                });

                Ok(RindexerEventFilter::Factory(FactoryFilter {
                    project_path: self.project_path.clone(),
                    indexer_name: self.indexer_name.clone(),
                    factory_contract_name: details.contract_name.clone(),
                    factory_address: details.address.clone(),
                    factory_event_name: details.event.name.clone(),
                    factory_input_name: details.input_name.clone(),
                    network: self.network_contract.network.clone(),
                    topic_id: self.topic_id,
                    topics: index_filter.cloned().map(Into::into).unwrap_or_default(),
                    clickhouse: self.clickhouse.clone(),
                    postgres: self.postgres.clone(),
                    csv_details: self.csv_details.clone(),

                    current_block: self.start_block,
                    next_block: self.end_block,
                }))
            }
        }
    }

    pub async fn trigger_event(&self, fn_data: Vec<EventResult>) -> Result<(), String> {
        self.registry.trigger_event(&self.id, fn_data).await
    }
}

pub struct FactoryEventProcessingConfig {
    pub id: String,
    pub project_path: PathBuf,
    pub indexer_name: String,
    pub contract_name: String,
    pub address: ValueOrArray<Address>,
    pub input_name: ValueOrArray<String>,
    pub event: Event,
    pub config: Config,
    pub network_contract: Arc<NetworkContract>,
    pub timestamps: bool,
    pub start_block: U64,
    pub end_block: U64,
    pub registry: Arc<EventCallbackRegistry>,
    pub progress: Arc<IndexingEventsProgressState>,
    pub postgres: Option<Arc<PostgresClient>>,
    pub clickhouse: Option<Arc<ClickhouseClient>>,
    pub csv_details: Option<CsvDetails>,
    pub stream_last_synced_block_file_path: Option<String>,
    pub index_event_in_order: bool,
    pub live_indexing: bool,
    pub indexing_distance_from_head: U64,
    /// Per-generation cancellation token for hot-reload support.
    pub cancel_token: CancellationToken,
    /// Derived/custom tables associated with this event (for reorg cleanup).
    pub tables: Arc<Vec<TableRuntime>>,
    /// Broadcast sender for reorg events (code-gen mode).
    pub reorg_sender: Option<Sender<ReorgEvent>>,
    /// Streams clients for reorg retraction (no-code mode).
    pub streams_clients: Arc<Option<StreamsClients>>,
    /// ABI path(s) for reconstructing raw events during replay.
    pub contract_abi: Option<StringOrArray>,
    /// Providers by network for replay parity with no-code table execution.
    pub providers: Arc<HashMap<String, Arc<dyn ChainProvider>>>,
    /// Constants for replay parity with no-code table execution.
    pub constants: Arc<Constants>,
    /// Multicall overrides by network for replay parity.
    pub multicall_addresses: Arc<HashMap<String, Option<String>>>,
}

impl FactoryEventProcessingConfig {
    pub fn input_names(&self) -> Vec<String> {
        match &self.input_name {
            ValueOrArray::Value(name) => vec![name.clone()],
            ValueOrArray::Array(names) => names.clone(),
        }
    }

    pub fn to_event_filter(&self) -> Result<RindexerEventFilter, BuildRindexerFilterError> {
        let event_name = self.event.name.clone();
        let event_selector = self.event.selector();

        let details = AddressDetails {
            address: self.address.clone(),
            indexed_filters: Some(vec![EventInputIndexedFilters {
                event_name: event_name.clone(),
                indexed_1: None,
                indexed_2: None,
                indexed_3: None,
            }]),
        };

        RindexerEventFilter::new_address_filter(
            &event_selector,
            &event_name,
            &details,
            self.start_block,
            self.end_block,
        )
    }

    pub async fn trigger_event(&self, events: Vec<EventResult>) -> Result<(), String> {
        self.registry.trigger_event(&self.id, events.clone()).await?;

        update_known_factory_deployed_addresses(self, &events).await.map_err(|e| e.to_string())
    }

    pub fn info_log_name(&self) -> String {
        format!("{}::{}::{}", self.contract_name, self.event.name, self.network_contract.network)
    }
}

pub enum EventProcessingConfig {
    ContractEventProcessing(ContractEventProcessingConfig),
    FactoryEventProcessing(FactoryEventProcessingConfig),
}

impl From<ContractEventProcessingConfig> for EventProcessingConfig {
    fn from(config: ContractEventProcessingConfig) -> Self {
        Self::ContractEventProcessing(config)
    }
}

impl From<FactoryEventProcessingConfig> for EventProcessingConfig {
    fn from(config: FactoryEventProcessingConfig) -> Self {
        Self::FactoryEventProcessing(config)
    }
}

impl EventProcessingConfig {
    pub fn is_factory_event(&self) -> bool {
        match self {
            Self::ContractEventProcessing(_) => false,
            Self::FactoryEventProcessing(_) => true,
        }
    }

    pub fn topic_id(&self) -> B256 {
        match self {
            Self::ContractEventProcessing(config) => config.topic_id,
            Self::FactoryEventProcessing(config) => config.event.selector(),
        }
    }

    pub fn processor_id(&self) -> B256 {
        let topic_id = self.topic_id();
        let contract_name = self.contract_name();
        let network = self.network_contract().network.to_string();

        let detail_key = self.detail_key();
        let combined = format!("{topic_id}{contract_name}{network}{detail_key}");
        keccak256(combined.as_bytes())
    }

    pub fn detail_key(&self) -> String {
        derive_detail_key(&self.network_contract().indexing_contract_setup, &self.event_name())
    }

    pub fn config(&self) -> &Config {
        match self {
            Self::ContractEventProcessing(config) => &config.config,
            Self::FactoryEventProcessing(config) => &config.config,
        }
    }

    pub fn timestamps(&self) -> bool {
        match self {
            Self::ContractEventProcessing(config) => config.timestamps,
            Self::FactoryEventProcessing(config) => config.timestamps,
        }
    }

    pub fn info_log_name(&self) -> String {
        match self {
            Self::ContractEventProcessing(config) => config.info_log_name(),
            Self::FactoryEventProcessing(config) => config.info_log_name(),
        }
    }

    pub fn network_contract(&self) -> Arc<NetworkContract> {
        match self {
            Self::ContractEventProcessing(config) => config.network_contract.clone(),
            Self::FactoryEventProcessing(config) => config.network_contract.clone(),
        }
    }

    pub fn index_event_in_order(&self) -> bool {
        match self {
            Self::ContractEventProcessing(config) => config.index_event_in_order,
            Self::FactoryEventProcessing(config) => config.index_event_in_order,
        }
    }

    pub fn contract_name(&self) -> String {
        match self {
            Self::ContractEventProcessing(config) => config.contract_name.clone(),
            Self::FactoryEventProcessing(config) => config.contract_name.clone(),
        }
    }

    pub fn indexer_name(&self) -> String {
        match self {
            Self::ContractEventProcessing(config) => config.indexer_name.clone(),
            Self::FactoryEventProcessing(config) => config.indexer_name.clone(),
        }
    }

    pub fn id(&self) -> &str {
        match self {
            Self::ContractEventProcessing(config) => &config.id,
            Self::FactoryEventProcessing(config) => &config.id,
        }
    }

    pub fn event_name(&self) -> String {
        match self {
            Self::ContractEventProcessing(config) => config.event_name.clone(),
            Self::FactoryEventProcessing(config) => config.event.name.clone(),
        }
    }

    pub fn live_indexing(&self) -> bool {
        match self {
            Self::ContractEventProcessing(config) => config.live_indexing,
            Self::FactoryEventProcessing(config) => config.live_indexing,
        }
    }

    pub fn indexing_distance_from_head(&self) -> U64 {
        match self {
            Self::ContractEventProcessing(config) => config.indexing_distance_from_head,
            Self::FactoryEventProcessing(config) => config.indexing_distance_from_head,
        }
    }

    pub fn progress(&self) -> Arc<IndexingEventsProgressState> {
        match self {
            Self::ContractEventProcessing(config) => config.progress.clone(),
            Self::FactoryEventProcessing(config) => config.progress.clone(),
        }
    }

    pub fn registry(&self) -> Arc<EventCallbackRegistry> {
        match self {
            Self::ContractEventProcessing(config) => config.registry.clone(),
            Self::FactoryEventProcessing(config) => config.registry.clone(),
        }
    }

    pub fn postgres(&self) -> Option<Arc<PostgresClient>> {
        match self {
            Self::ContractEventProcessing(config) => config.postgres.clone(),
            Self::FactoryEventProcessing(config) => config.postgres.clone(),
        }
    }

    pub fn clickhouse(&self) -> Option<Arc<ClickhouseClient>> {
        match self {
            Self::ContractEventProcessing(config) => config.clickhouse.clone(),
            Self::FactoryEventProcessing(config) => config.clickhouse.clone(),
        }
    }

    pub fn tables(&self) -> Arc<Vec<TableRuntime>> {
        match self {
            Self::ContractEventProcessing(config) => config.tables.clone(),
            Self::FactoryEventProcessing(config) => config.tables.clone(),
        }
    }

    pub fn reorg_sender(&self) -> Option<Sender<ReorgEvent>> {
        match self {
            Self::ContractEventProcessing(config) => config.reorg_sender.clone(),
            Self::FactoryEventProcessing(config) => config.reorg_sender.clone(),
        }
    }

    pub fn streams_clients(&self) -> Arc<Option<StreamsClients>> {
        match self {
            Self::ContractEventProcessing(config) => config.streams_clients.clone(),
            Self::FactoryEventProcessing(config) => config.streams_clients.clone(),
        }
    }

    pub fn contract_abi(&self) -> Option<StringOrArray> {
        match self {
            Self::ContractEventProcessing(config) => config.contract_abi.clone(),
            Self::FactoryEventProcessing(config) => config.contract_abi.clone(),
        }
    }

    pub fn providers(&self) -> Arc<HashMap<String, Arc<dyn ChainProvider>>> {
        match self {
            Self::ContractEventProcessing(config) => config.providers.clone(),
            Self::FactoryEventProcessing(config) => config.providers.clone(),
        }
    }

    pub fn constants(&self) -> Arc<Constants> {
        match self {
            Self::ContractEventProcessing(config) => config.constants.clone(),
            Self::FactoryEventProcessing(config) => config.constants.clone(),
        }
    }

    pub fn multicall_addresses(&self) -> Arc<HashMap<String, Option<String>>> {
        match self {
            Self::ContractEventProcessing(config) => config.multicall_addresses.clone(),
            Self::FactoryEventProcessing(config) => config.multicall_addresses.clone(),
        }
    }

    pub fn csv_details(&self) -> Option<CsvDetails> {
        match self {
            Self::ContractEventProcessing(config) => config.csv_details.clone(),
            Self::FactoryEventProcessing(config) => config.csv_details.clone(),
        }
    }

    pub fn stream_last_synced_block_file_path(&self) -> Option<String> {
        match self {
            Self::ContractEventProcessing(config) => {
                config.stream_last_synced_block_file_path.clone()
            }
            Self::FactoryEventProcessing(config) => {
                config.stream_last_synced_block_file_path.clone()
            }
        }
    }

    pub fn project_path(&self) -> PathBuf {
        match self {
            Self::ContractEventProcessing(config) => config.project_path.clone(),
            Self::FactoryEventProcessing(config) => config.project_path.clone(),
        }
    }

    pub fn cancel_token(&self) -> &CancellationToken {
        match self {
            Self::ContractEventProcessing(config) => &config.cancel_token,
            Self::FactoryEventProcessing(config) => &config.cancel_token,
        }
    }

    pub fn to_event_filter(&self) -> Result<RindexerEventFilter, BuildRindexerFilterError> {
        match self {
            Self::ContractEventProcessing(config) => config.to_event_filter(),
            Self::FactoryEventProcessing(config) => config.to_event_filter(),
        }
    }

    pub async fn trigger_event(&self, fn_data: Vec<EventResult>) -> Result<(), String> {
        match self {
            Self::ContractEventProcessing(config) => config.trigger_event(fn_data).await,
            Self::FactoryEventProcessing(config) => config.trigger_event(fn_data).await,
        }
    }
}

#[cfg(test)]
mod detail_key_tests {
    use super::{derive_detail_key, LEGACY_DETAIL_KEY};
    use crate::{
        event::contract_setup::{AddressDetails, FilterDetails, IndexingContractSetup},
        manifest::contract::EventInputIndexedFilters,
    };
    use alloy::{primitives::Address, rpc::types::ValueOrArray};

    fn indexed_filter(position: usize, value: &str) -> EventInputIndexedFilters {
        let mut filter = EventInputIndexedFilters {
            event_name: "Transfer".to_string(),
            indexed_1: None,
            indexed_2: None,
            indexed_3: None,
        };
        match position {
            1 => filter.indexed_1 = Some(vec![value.to_string()]),
            2 => filter.indexed_2 = Some(vec![value.to_string()]),
            3 => filter.indexed_3 = Some(vec![value.to_string()]),
            _ => unreachable!(),
        }
        filter
    }

    #[test]
    fn address_detail_key_preserves_the_fiet_fixture() {
        let setup = IndexingContractSetup::Address(AddressDetails {
            address: ValueOrArray::Value(Address::repeat_byte(0xab)),
            indexed_filters: Some(vec![indexed_filter(
                2,
                "0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
            )]),
        });

        assert_eq!(
            derive_detail_key(&setup, "Transfer"),
            "0xabababababababababababababababababababab:i2:0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
        );
    }

    #[test]
    fn pool_manager_swap_detail_key_matches_the_maker_client_fixture_byte_for_byte() {
        let setup = IndexingContractSetup::Address(AddressDetails {
            address: ValueOrArray::Value(Address::repeat_byte(0x11)),
            indexed_filters: Some(vec![EventInputIndexedFilters {
                event_name: "Swap".to_string(),
                indexed_1: Some(vec![
                    "0x1111111111111111111111111111111111111111111111111111111111111111"
                        .to_string(),
                    "0x2222222222222222222222222222222222222222222222222222222222222222"
                        .to_string(),
                ]),
                indexed_2: None,
                indexed_3: None,
            }]),
        });

        assert_eq!(
            derive_detail_key(&setup, "Swap"),
            "0x1111111111111111111111111111111111111111:i1:0x1111111111111111111111111111111111111111111111111111111111111111,0x2222222222222222222222222222222222222222222222222222222222222222"
        );
    }

    #[test]
    fn wallet_transfer_detail_keys_are_unique_across_tokens_and_indexed_positions() {
        let wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let keys =
            [Address::repeat_byte(0x01), Address::repeat_byte(0x02), Address::repeat_byte(0x03)]
                .into_iter()
                .flat_map(|token| {
                    [1, 2].map(|position| {
                        derive_detail_key(
                            &IndexingContractSetup::Address(AddressDetails {
                                address: ValueOrArray::Value(token),
                                indexed_filters: Some(vec![indexed_filter(position, wallet)]),
                            }),
                            "Transfer",
                        )
                    })
                })
                .collect::<std::collections::HashSet<_>>();

        assert_eq!(keys.len(), 6);
    }

    #[test]
    fn filter_mode_detail_has_an_exact_nonlegacy_key() {
        let setup = IndexingContractSetup::Filter(FilterDetails {
            events: ValueOrArray::Value("Transfer".to_string()),
            indexed_filters: Some(vec![indexed_filter(1, "wallet")]),
        });

        assert_eq!(derive_detail_key(&setup, "Transfer"), "filter:transfer:i1:wallet");
    }

    #[test]
    fn filter_mode_detail_key_includes_every_matching_alternative_in_manifest_order() {
        let setup = IndexingContractSetup::Filter(FilterDetails {
            events: ValueOrArray::Value("Transfer".to_string()),
            indexed_filters: Some(vec![
                indexed_filter(1, "from-wallet"),
                indexed_filter(2, "to-wallet"),
                EventInputIndexedFilters {
                    event_name: "Approval".to_string(),
                    indexed_1: Some(vec!["owner".to_string()]),
                    indexed_2: None,
                    indexed_3: None,
                },
            ]),
        });

        assert_eq!(
            derive_detail_key(&setup, "Transfer"),
            "filter:transfer:i1:from-wallet|i2:to-wallet"
        );
    }

    #[test]
    fn unfiltered_single_stream_uses_the_explicit_legacy_identity() {
        let setup = IndexingContractSetup::Address(AddressDetails {
            address: ValueOrArray::Value(Address::ZERO),
            indexed_filters: None,
        });

        assert_eq!(derive_detail_key(&setup, "Transfer"), LEGACY_DETAIL_KEY);
    }
}

#[derive(Clone)]
pub struct TraceProcessingConfig {
    pub id: String,
    pub chain_id: u64,
    pub project_path: PathBuf,
    pub start_block: U64,
    pub end_block: U64,
    pub indexer_name: String,
    pub contract_name: String,
    pub event_name: String,
    pub network: String,
    pub progress: Arc<IndexingEventsProgressState>,
    pub postgres: Option<Arc<PostgresClient>>,
    pub csv_details: Option<CsvDetails>,
    pub registry: Arc<TraceCallbackRegistry>,
    pub method: TraceProcessingMethod,
    pub stream_last_synced_block_file_path: Option<String>,
    /// Per-generation cancellation token for hot-reload support.
    pub cancel_token: CancellationToken,
}

impl TraceProcessingConfig {
    pub async fn trigger_event(&self, fn_data: Vec<TraceResult>) -> Result<(), String> {
        // Trigger events for all registered events in this network's registry
        for event in &self.registry.events {
            self.registry.trigger_event(&event.id, fn_data.clone()).await?;
        }
        Ok(())
    }
}
