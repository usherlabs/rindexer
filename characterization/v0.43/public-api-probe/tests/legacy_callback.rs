use rindexer::{EthereumSqlTypeWrapper, PostgresClient};
use testcontainers::runners::AsyncRunner;
use testcontainers_modules::postgres::Postgres;

#[tokio::test]
async fn custom_handler_crash_window_redelivers_and_requires_idempotence() {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let postgres = Postgres::default().start().await.expect("failed to start Postgres");
    let port = postgres.get_host_port_ipv4(5432).await.expect("missing Postgres port");
    std::env::set_var(
        "DATABASE_URL",
        format!("postgresql://postgres:postgres@127.0.0.1:{port}/postgres"),
    );

    let client = PostgresClient::new().await.expect("failed to connect to Postgres");
    client
        .batch_execute(
            "CREATE TABLE unsafe_events (value NUMERIC NOT NULL);\
             CREATE TABLE idempotent_events (value NUMERIC PRIMARY KEY);\
             CREATE SCHEMA rindexer_internal;\
             CREATE TABLE rindexer_internal.test_cursor (\
                 network TEXT PRIMARY KEY, last_synced_block NUMERIC NOT NULL\
             );\
             INSERT INTO rindexer_internal.test_cursor VALUES ('anvil', 0);",
        )
        .await
        .expect("failed to initialize probe tables");

    let columns = vec!["value".to_string()];
    let callback_rows = vec![vec![EthereumSqlTypeWrapper::U64(7)]];

    // First callback persistence succeeds. The injected crash is the deliberate
    // absence of a cursor update before the client/process is discarded.
    client
        .insert_bulk("unsafe_events", &columns, &callback_rows)
        .await
        .expect("first non-idempotent callback write failed");
    client
        .batch_execute("INSERT INTO idempotent_events VALUES (7) ON CONFLICT DO NOTHING")
        .await
        .expect("first idempotent callback write failed");
    drop(client);

    // Restart observes the old cursor and therefore redelivers the callback.
    let restarted = PostgresClient::new().await.expect("failed to reconnect after crash");
    let cursor: i64 = restarted
        .query_one(
            "SELECT last_synced_block::bigint FROM rindexer_internal.test_cursor WHERE network = 'anvil'",
            &[],
        )
        .await
        .expect("failed to read cursor")
        .get(0);
    assert_eq!(cursor, 0, "callback persistence alone must not advance the cursor");

    restarted
        .insert_bulk("unsafe_events", &columns, &callback_rows)
        .await
        .expect("redelivered non-idempotent callback write failed");
    restarted
        .batch_execute("INSERT INTO idempotent_events VALUES (7) ON CONFLICT DO NOTHING")
        .await
        .expect("redelivered idempotent callback write failed");

    let unsafe_count: i64 = restarted
        .query_one("SELECT count(*) FROM unsafe_events", &[])
        .await
        .expect("failed to count unsafe rows")
        .get(0);
    let idempotent_count: i64 = restarted
        .query_one("SELECT count(*) FROM idempotent_events", &[])
        .await
        .expect("failed to count idempotent rows")
        .get(0);
    assert_eq!(unsafe_count, 2, "legacy callback redelivery can duplicate rows");
    assert_eq!(idempotent_count, 1, "handler-owned idempotence absorbs redelivery");
}
