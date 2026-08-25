//! Process probe for the caller-owned embedded lifecycle.

use std::{path::PathBuf, process::ExitCode, time::Duration};

use rindexer::{
    indexer::task_tracker::indexing_event_processing, initiate_shutdown, setup_info_logger,
    start_rindexer_no_code_embedded, GraphqlOverrideSettings, IndexerNoCodeDetails,
    StartNoCodeDetails,
};

#[cfg(unix)]
async fn caller_signal() -> Result<&'static str, String> {
    use tokio::signal::unix::{signal, SignalKind};

    let mut sigterm = signal(SignalKind::terminate()).map_err(|error| error.to_string())?;
    let mut sigint = signal(SignalKind::interrupt()).map_err(|error| error.to_string())?;
    tokio::select! {
        _ = sigterm.recv() => Ok("SIGTERM"),
        _ = sigint.recv() => Ok("SIGINT"),
    }
}

#[cfg(windows)]
async fn caller_signal() -> Result<&'static str, String> {
    tokio::signal::ctrl_c().await.map_err(|error| error.to_string())?;
    Ok("Ctrl+C")
}

#[tokio::main]
async fn main() -> ExitCode {
    setup_info_logger();
    let mut arguments = std::env::args().skip(1);
    let Some(manifest_path) = arguments.next().map(PathBuf::from) else {
        eprintln!("usage: embedded_lifecycle_probe <manifest> [--force-timeout]");
        return ExitCode::from(64);
    };
    let force_timeout = arguments.any(|argument| argument == "--force-timeout");

    let engine = start_rindexer_no_code_embedded(StartNoCodeDetails {
        manifest_path: &manifest_path,
        indexing_details: IndexerNoCodeDetails { enabled: true },
        graphql_details: GraphqlOverrideSettings { enabled: false, override_port: None },
        watch: false,
    });
    tokio::pin!(engine);

    let signal = tokio::select! {
        signal = caller_signal() => match signal {
            Ok(signal) => signal,
            Err(error) => {
                eprintln!("FIET_EMBEDDED_SIGNAL_ERROR={error}");
                return ExitCode::from(65);
            }
        },
        result = &mut engine => {
            eprintln!("FIET_EMBEDDED_UNEXPECTED_ENGINE_COMPLETION={result:?}");
            return ExitCode::from(66);
        }
    };
    println!("FIET_EMBEDDED_CALLER_SIGNAL={signal}");

    if force_timeout {
        indexing_event_processing();
    }
    match tokio::time::timeout(Duration::from_millis(500), initiate_shutdown()).await {
        Ok(()) => println!("FIET_EMBEDDED_DRAIN=complete"),
        Err(_) => {
            eprintln!("FIET_EMBEDDED_DRAIN=timeout");
            return ExitCode::from(42);
        }
    }

    match tokio::time::timeout(Duration::from_secs(5), &mut engine).await {
        Ok(Ok(())) => {
            println!("FIET_EMBEDDED_ENGINE=complete");
            ExitCode::SUCCESS
        }
        Ok(Err(error)) => {
            eprintln!("FIET_EMBEDDED_ENGINE_ERROR={error}");
            ExitCode::from(67)
        }
        Err(_) => {
            eprintln!("FIET_EMBEDDED_ENGINE=timeout");
            ExitCode::from(68)
        }
    }
}
