# Choosing a text-to-speech engine: a measured bake-off

Daily Brief needs two voices reading about 12,000 characters per episode, for at most $0.15 a day. The price sheet doesn't answer that question; the bill does.

## Method

- **Input:** the first 8 turns (1,047 characters, two speakers) of a script the builder had written, identical for every
  model. The script was derived from a third-party podcast, so the text is not published here; any similar 8-turn
  dialogue reproduces the shape of the result.
- **Requests:** made the way production makes them. Models that accept a whole dialogue in one request (Gemini
  TTS, Fish Audio) got one request. Every other model got one request per turn.
- **Cost:** read back per request from OpenRouter's `/api/v1/generation?id=<x-generation-id>` (`total_cost`),
  never computed from the listed price.
- **Accuracy:** word error rate of the audio transcribed back with faster-whisper `small.en` against the script.
- **Pace:** words per minute of the rendered audio.
- **Quality:** judged by ear from the same excerpt (not reproducible from this repo; the audio is not published).

## Results (2026-10-25)

| Model | Requests | Billed for 1,047 chars | Billed $/1M chars | Listed $/1M chars | WER | Words/min |
|---|---|---|---|---|---|---|
| Fish Audio S2.1 Pro Free | 1 | $0.000000 | 0 | 0 | 5.1% | 144 |
| Fish Audio Drama 3 | 1 | $0.002318 | 2.2 | 2 | 2.0% | **94** |
| Kokoro 82M | 8 | $0.004188 | **4.0** | **0.62** | 3.0% | 186 |
| **HeyGen Voice 1 Turbo** ✔ | 8 | $0.004188 | 4.0 | 4 | 3.6% | 170 |
| Gemini 3.8 Flash Lite TTS | 1 | $0.010618 | 10.1 | per audio token | 4.6% | 169 |
| Orpheus 3B | 8 | $0.015705 | 15.0 | 7 | 6.6% | 165 |
| MAI Voice 2.1 Flash | 8 | $0.015705 | 15.0 | 15 | 3.0% | 152 |
| Grok Voice TTS 1.0 | 8 | $0.015705 | 15.0 | 15 | 3.6% | 166 |
| Gemini 3.8 Flash TTS | 1 | $0.016834 | 16.1 | per audio token | 3.0% | 159 |
| Fish Audio S2.1 Pro | 1 | $0.017385 | 16.6 | 15 | 3.6% | 151 |

## What the numbers taught us

1. **Listed ≠ billed.** Kokoro lists at $0.62 per million characters and billed $4.00, about 6×. Short,
   per-turn requests hit a per-request floor, so the cost of a dialogue depends on how many requests it takes, not just how
   many characters it has. Orpheus showed the same gap (7 listed, 15 billed).
2. **Every engine was accurate enough.** All ten stayed between 2% and 7% WER, mostly punctuation and proper names. Accuracy
   didn't separate them; voice quality and price did.
3. **Pace is a hidden cost.** Drama 3 is the cheapest per character, but it performs at 94 words a minute, so a
   15-minute script becomes a 28-minute segment.
4. **One request per dialogue** (Gemini, Fish) avoids the per-request floor and keeps prosody consistent across
   turns. It is the better shape when the budget allows.

## Choice

HeyGen Voice 1 Turbo: the pick by ear among the models under budget. At about $4 per million characters a
12,000-character episode costs about **$0.05**, so the default $0.15 daily cap allows about three episodes. There is no
backup engine: when the provider fails, the episode fails and the hourly builder retries later.
