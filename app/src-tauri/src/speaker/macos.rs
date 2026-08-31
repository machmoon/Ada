// Pluely macos speaker input and stream.
//
// FORK CHANGE (2026-08-31, Silkscreen): the upstream implementation of this file
// captured system audio through a CoreAudio process tap built on the `cidre`
// crate. `cidre`'s build script shells out to `xcodebuild`, so the whole crate
// graph refuses to compile on a machine that has only the Command Line Tools
// installed — `cargo build` dies before it ever reaches our code. We dropped the
// dependency rather than require a ~10 GB Xcode install for every contributor.
//
// The module is stubbed, not deleted, on purpose. `mod.rs` selects a platform
// implementation from this file and `lib.rs` registers eleven Tauri commands on
// top of it; removing the module would move a loud compile error into a silent
// runtime one, where the React UI invokes commands that no longer exist and
// fails somewhere nobody connects back to this change.
//
// The rule the stub follows: system-audio capture reports an honest error. It
// never returns fake success, an empty buffer, or silence — a capture path that
// pretends to work would send the model transcripts of nothing at all, which is
// far harder to diagnose than a refusal. Device enumeration, by contrast, is
// real: it runs through `cpal`, which talks to CoreAudio without needing Xcode,
// so the microphone-selection UI keeps working.
use super::AudioDevice;
use anyhow::Result;
use cpal::traits::{DeviceTrait, HostTrait};
use futures_util::Stream;
use std::task::Poll;

// The frontend treats the literal id "default" as "let the browser choose the
// device" (see AudioRecorder.tsx and AutoSpeechVad.tsx, which only build an
// `{ deviceId: { exact } }` constraint for ids other than "default"). Reporting
// the system default under that id keeps the common case working; any other
// device is identified by its CoreAudio name, which is the only stable handle
// cpal exposes.
const DEFAULT_DEVICE_ID: &str = "default";

fn collect_devices<I: Iterator<Item = cpal::Device>>(
    devices: I,
    default_name: Option<&str>,
) -> Vec<AudioDevice> {
    devices
        .filter_map(|device| {
            // A device whose name cannot be read cannot be selected or
            // displayed, so drop it rather than inventing a placeholder that
            // the frontend would later fail to match.
            let name = device.name().ok()?;
            let is_default = default_name == Some(name.as_str());
            Some(AudioDevice {
                id: if is_default {
                    DEFAULT_DEVICE_ID.to_string()
                } else {
                    name.clone()
                },
                name,
                is_default,
            })
        })
        .collect()
}

pub fn get_input_devices() -> Result<Vec<AudioDevice>> {
    let host = cpal::default_host();
    let default_name = host.default_input_device().and_then(|d| d.name().ok());
    Ok(collect_devices(
        host.input_devices()?,
        default_name.as_deref(),
    ))
}

pub fn get_output_devices() -> Result<Vec<AudioDevice>> {
    let host = cpal::default_host();
    let default_name = host.default_output_device().and_then(|d| d.name().ok());
    Ok(collect_devices(
        host.output_devices()?,
        default_name.as_deref(),
    ))
}

// Uninhabited: `new` below always returns `Err`, so a `SpeakerInput` can never
// exist on this build. Encoding that in the type makes `stream()` — which
// `mod.rs` declares as infallible and so cannot be made to return an error —
// statically unreachable, rather than a `panic!` waiting for someone to find a
// path to it.
pub struct SpeakerInput {
    never: std::convert::Infallible,
}

impl SpeakerInput {
    pub fn new(_device_id: Option<String>) -> Result<Self> {
        Err(anyhow::anyhow!(
            "system audio capture is not built into this macOS build: the CoreAudio \
             tap was removed because its `cidre` dependency requires a full Xcode \
             install to compile. Microphone input and device selection still work."
        ))
    }

    pub fn stream(self) -> SpeakerStream {
        // No value of `Infallible` exists, so this match has no arms to write
        // and the compiler proves the call site is dead.
        match self.never {}
    }
}

// Never constructed — see `SpeakerInput::stream`. It exists only to satisfy the
// `PlatformSpeakerStream` alias that `mod.rs` names unconditionally.
pub struct SpeakerStream {
    never: std::convert::Infallible,
}

impl SpeakerStream {
    pub fn sample_rate(&self) -> u32 {
        match self.never {}
    }
}

impl Stream for SpeakerStream {
    type Item = f32;

    fn poll_next(
        self: std::pin::Pin<&mut Self>,
        _cx: &mut std::task::Context<'_>,
    ) -> Poll<Option<Self::Item>> {
        match self.never {}
    }
}
