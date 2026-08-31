fn main() {
    // Upstream baked four build-time secrets in here -- PAYMENT_ENDPOINT,
    // API_ACCESS_KEY, APP_ENDPOINT and POSTHOG_API_KEY -- read back via
    // `option_env!` from the licensing, SaaS and telemetry modules. Those
    // modules are gone, so nothing reads the values and injecting them would
    // only bake dead secrets into the binary.
    tauri_build::build()
}
