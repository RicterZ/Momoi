# NapCat Voice Call Bridge

A Linux Docker image that adds QQ voice calls to NapCat for Momoi. It combines NapCat, the QQ AVSDK bridge, and an audio broker while retaining NapCat's existing messaging support.

## Features

- Automatically answers incoming calls from the configured owner when Momoi is ready.
- Routes incoming speech through Momoi's ASR, Planner, and Replyer, then plays synthesized replies back into the call.
- Shares conversation history with regular QQ messages.
- Streams reply audio and prefetches subsequent speech bubbles for sequential playback.
- Interrupts playback when the owner speaks and cancels pending audio when the call ends.
- Supports running Momoi and the bridge on separate hosts.

## Components

- **NapCat:** QQ messaging and call events.
- **AVSDK bridge:** Native QQ call handling and audio routing, based on `RicterZ/maibot-qq-voice-call`.
- **Audio broker:** Authenticated connection to Momoi for incoming audio, playback, and call status.

Momoi supplies speech recognition, reply generation, and speech synthesis. This image contains the Linux call infrastructure; it does not provide standalone conversational behavior.
