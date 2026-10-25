import json
from types import SimpleNamespace as S

import pytest

from daily_brief import audio_qa
from tests.conftest import FakeASR, write_config

SCRIPT = ("So the first thing to measure is the trace, not the final answer. Then you look at where it "
          "went.")  # 20 words


def library_item(tmp_path, turns_text=SCRIPT):
    d = tmp_path / "lib" / "7"
    d.mkdir(parents=True, exist_ok=True)
    (d / "episode.mp3").write_bytes(b"mp3")
    half = len(turns_text.split()) // 2
    words = turns_text.split()
    turns = [{"speaker": "A", "text": " ".join(words[:half])}, {"speaker": "B", "text": " ".join(words[half:])}]
    (d / "episode.json").write_text(json.dumps({"turns": turns}))
    return d


def fake_asr(text, cost=0.002):
    words = text.split()
    return FakeASR([S(start=0, text=" ".join(words[:7])), S(start=1, text=" " + " ".join(words[7:]))], cost=cost)


def test_exact_match_is_zero(tmp_path):
    d = library_item(tmp_path)
    w = fake_asr(SCRIPT.upper().replace(",", "").replace(".", ""))  # case and punctuation do not count
    assert audio_qa.check_episode(d, w) == {"wer": 0.0, "words": 20, "cost_usd": 0.002}
    assert w.calls == [d / "episode.mp3"]


def test_one_substitution_in_twenty_words_is_five_percent(tmp_path):
    assert len(SCRIPT.split()) == 20
    r = audio_qa.check_episode(library_item(tmp_path), fake_asr(SCRIPT.replace("trace", "track")))
    assert r == {"wer": 0.05, "words": 20, "cost_usd": 0.002}


def test_insertions_and_deletions_count_and_numbers_stay_as_words():
    assert audio_qa.wer("a b c d", "a c d")[0] == 0.25
    assert audio_qa.wer("a b c d", "a b x c d")[0] == 0.25
    assert audio_qa.wer("it costs 20 dollars", "it costs twenty dollars")[0] == 0.25
    assert audio_qa.wer("game-changer, don't", "game changer dont")[0] == 0.0
    with pytest.raises(ValueError, match="no words"):
        audio_qa.wer("...", "anything")


def run_main(tmp_path, spoken, max_wer_line=None, capsys=None):
    cfg = write_config(tmp_path, {"max_wer = 0.05": max_wer_line} if max_wer_line else None)
    return audio_qa.main(["audio_qa", str(cfg), str(library_item(tmp_path))], lambda: fake_asr(spoken))


def test_exit_code_follows_the_threshold(tmp_path, capsys):
    off_by_one = SCRIPT.replace("trace", "track")  # wer 0.05: not over the 0.05 limit
    assert run_main(tmp_path, off_by_one) == 0
    assert json.loads(capsys.readouterr().out) == {"wer": 0.05, "words": 20, "cost_usd": 0.002}
    assert run_main(tmp_path, off_by_one, "max_wer = 0.04") == 1
    assert run_main(tmp_path, SCRIPT) == 0


def test_usage_error(capsys):
    assert audio_qa.main(["audio_qa"]) == 2


def test_a_provider_failure_gives_no_verdict(tmp_path, capsys):
    from daily_brief import asr
    cfg = write_config(tmp_path)
    broken = FakeASR(fail_on_call=1, fail_with=asr.TranscriptionUnavailable("ASR HTTP 402: no credit"))
    assert audio_qa.main(["audio_qa", str(cfg), str(library_item(tmp_path))], lambda: broken) == 2
    assert "HTTP 402" in capsys.readouterr().err
